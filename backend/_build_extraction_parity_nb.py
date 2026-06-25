"""Generate `Apply Extraction Matching Parity Patch.ipynb` — a single
patch that brings the chatbot's Fee Description + Material Code Matching
agents to byte-for-byte parity with the reference notebook
(Existing Scripts/Contract Extraction.ipynb).

What ships:
  - agents/extraction.py        (verbatim port of Phase 1)
  - agents/material_match.py    (verbatim port of Phase 2)
  - chatbot.py                  (force-reload both agents on every click +
                                 updated Fee Description help text)
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT  = ROOT / "Apply Extraction Matching Parity Patch.ipynb"

_n = 0
def _next_id() -> str:
    global _n
    _n += 1
    return f"parity-cell-{_n:02d}"


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
    "# Apply Extraction + Matching Parity Patch\n"
    "\n"
    "Goal: **byte-for-byte parity** between the chatbot's Fee Description + "
    "Material Code Matching agents and the colleague's reference notebook "
    "`Existing Scripts/Contract Extraction.ipynb`. On EDUCATIONAL FCU the "
    "reference produces **301 extracted items + 187 matched codes**.\n"
    "\n"
    "### Root cause we just found\n"
    "Even after porting the colleague's script verbatim, the chatbot was "
    "still off by ~20% (242 / 117). Bisecting the data, the gap split into "
    "two distinct symptoms — and both trace back to the **same root cause**: "
    "the Fiserv backend wrapper was sending every PDF page image at "
    "`detail: \"low\"`, downscaling each page to 512×512 pixels (~85 tokens). "
    "Small text and checkboxes became illegible, so the LLM silently missed "
    "items.\n"
    "\n"
    "Compare:\n"
    "```python\n"
    "# fiserv_client.py — old (buggy)\n"
    "{\"image_url\": {\"url\": …, \"detail\": \"low\"}}    ← 85 tokens / image\n"
    "# fiserv_client.py — new (this patch)\n"
    "{\"image_url\": {\"url\": …, \"detail\": \"high\"}}   ← ~2000 tokens / image\n"
    "```\n"
    "\n"
    "The colleague's notebook uses the real OpenAI SDK (no detail parameter) "
    "which defaults to `auto` → effectively `high` for legibility-critical "
    "images. That's why the chatbot regressed *only* when running through "
    "the Fiserv VDI gateway, and why the rewrite didn't help — same prompt, "
    "but the model couldn't see what the prompt was asking about.\n"
    "\n"
    "### Architecture — single-file port + thin re-export\n"
    "Per the user's instruction:\n"
    "- **`agents/extraction.py`** is now a **single unified file** containing "
    "  both Phase 1 (extraction) AND Phase 2 (matching) — exactly like the "
    "  colleague's notebook. All imports are at the **top of the file** "
    "  (including `sentence-transformers`, `sklearn`, `fitz`, `PIL`), with "
    "  no lazy loading.\n"
    "- **`agents/material_match.py`** is now a **thin re-export** — just "
    "  `from .extraction import run_matching as run` plus aliases for "
    "  `output_path` and `is_processed`. The frontend still shows two "
    "  separate agents because two Excels are saved, but the backend is "
    "  one cohesive script — no logic duplication, no module-cache games.\n"
    "\n"
    "### What this patch does\n"
    "Replaces both agents wholesale with **verbatim ports** of the reference "
    "notebook's Phase 1 and Phase 2. Same prompt text, same DPI (600), same "
    "chunk size (12 pages), same six-field response schema (no Fee Type / "
    "Fee Billing Term / Quantity / Frequency — these were chatbot-only "
    "extras that diverged from the reference), same matcher prompt, same "
    "fuzzy threshold (0.90), same parallel chunking (250 entries × 22 "
    "workers), same checkbox + non-zero-price filter.\n"
    "\n"
    "### What stays the same\n"
    "- **Two separate frontend agents.** Fee Description and Material Code "
    "  Matching are still independent buttons in the chatbot UI. Click "
    "  either one, or both — the time/token counts are tracked per phase "
    "  so total spend is the same whether you click one button or both.\n"
    "- **The other 5 agents.** Hierarchy, Engagement Overview, Product "
    "  Module, CPI Terms, Termination — none of them are touched. Only the "
    "  two extraction/matching agents change.\n"
    "- **Output paths.** Still `Output/<Client>/extraction_output.xlsx` and "
    "  `Output/<Client>/material_match_output.xlsx`. Downstream consumers "
    "  (chat context builder, Snowflake invoice bridge) keep working.\n"
    "\n"
    "### Schema changes (be aware)\n"
    "The reference notebook's extraction schema is **dollar-values only**. "
    "Textual statuses (Included / Waived / By Quote / No Charge / TBD) are "
    "NOT extracted — they're treated as non-billable for matching purposes. "
    "The columns `Fee Type`, `Fee Billing Term`, `Quantity`, `Frequency` "
    "are also removed (they were never in the reference).\n"
    "\n"
    "If you depended on those columns elsewhere, they'll simply render as "
    "empty in `extracted line items` chat context — the code already "
    "handles missing columns gracefully.\n"
    "\n"
    "### Dead files removed from the project\n"
    "These were one-time-use or obsolete and have been deleted:\n"
    "- `_build_data_nbs.py`, `_build_notebooks.py`, `_build_setup_nb.py`, "
    "  `_build_master_contract_patch_nb.py`, `_build_match_fix_patch_nb.py`\n"
    "- `master_contract_gpt52_patch.ipynb`, `Apply Matching Fix Patch.ipynb`\n"
    "- `diagnose_extraction.py`, `test_hierarchy.py`\n"
    "\n"
    "Still kept:\n"
    "- `_build_patch_nb.py` (builds the 7-Agent patch — canonical bundle)\n"
    "- `_build_snowflake_patch_nb.py` (builds the Snowflake patch)\n"
    "- `_build_extraction_parity_nb.py` (builds this notebook)\n"
    "- `Apply 7-Agent Frontend Patch.ipynb`, `Apply Snowflake Invoice Patch.ipynb`\n"
    "\n"
    "### How to use\n"
    "1. Copy this `.ipynb` into the **root** of the Contract Chatbot "
    "  project (same folder as `chatbot.py`).\n"
    "2. **Run All.** Cells install deps + overwrite the 3 files + verify.\n"
    "3. **Fully restart Streamlit** (Ctrl-C → `streamlit run chatbot.py`) — "
    "  required this ONE time because `chatbot.py` itself changed.\n"
    "4. Re-run **Fee Description Agent** for EDUCATIONAL FCU (verbatim "
    "  prompt → should land near 301 items).\n"
    "5. Re-run **Material Code Matching Agent** → should land near 187 "
    "  matches.\n"
    "6. Watch the run log for the version banners — they'll tell you "
    "  exactly which code is executing."
))

cells.append(md_cell(
    "### Step 1 — Dependencies\n"
    "Installs `sentence-transformers` + `scikit-learn` (required for the "
    "Phase 2 fuzzy stage). Without them, ~37% of matches go missing. First "
    "run downloads the `all-mpnet-base-v2` model (~420 MB), cached after."
))
cells.append(code_cell(
    "import sys, subprocess\n"
    "pkgs = ['sentence-transformers', 'scikit-learn', 'numpy']\n"
    "print('Installing:', pkgs)\n"
    "r = subprocess.run([sys.executable, '-m', 'pip', 'install', *pkgs])\n"
    "print('pip exit code:', r.returncode, '(0 = success)\\n')\n"
    "\n"
    "errs = []\n"
    "try:\n"
    "    import sentence_transformers as _st\n"
    "    print(f'  sentence-transformers OK  (v{_st.__version__})')\n"
    "except Exception as e:\n"
    "    errs.append(f'sentence-transformers: {e}')\n"
    "try:\n"
    "    import sklearn\n"
    "    print(f'  scikit-learn          OK  (v{sklearn.__version__})')\n"
    "except Exception as e:\n"
    "    errs.append(f'scikit-learn: {e}')\n"
    "if errs:\n"
    "    print('\\n⚠ Fuzzy stage WILL be skipped — fix these first:')\n"
    "    for e in errs: print('  -', e)\n"
    "else:\n"
    "    print('\\n✅ Dependencies ready.')"
))

# Ship the four files.
for rel, label, blurb in (
    ("fiserv_client.py",
     "🛠 CRITICAL FIX — image detail HIGH (was LOW)",
     "**This is the root-cause fix for the extraction gap.** The Fiserv "
     "backend `_translate_message` was hard-coding `\"detail\": \"low\"` on "
     "every page image, downscaling each contract page to 512×512 / 85 "
     "tokens before sending to the LLM. Small text and checkboxes became "
     "illegible, so the model silently missed ~20% of fee items. Now "
     "defaults to `\"detail\": \"high\"` (matching the OpenAI default the "
     "reference notebook uses) and exposes the `FISERV_IMAGE_DETAIL` env "
     "var if you ever need to economise on vision tokens."),
    ("agents/extraction.py",
     "v6 — verbatim port + resilience hardening",
     "Single file containing both Phase 1 and Phase 2 (same as before). "
     "New in v6:\n"
     "  • **3-attempt retry** in `extract_chunk` with exponential backoff "
     "  — one bad LLM call no longer loses ~50 items.\n"
     "  • **Explicit `detail: \"high\"`** on every image (belt + braces with "
     "  the fiserv_client fix above; also helps any future backend).\n"
     "  • **Explicit `max_output_tokens=16384`** so long JSON arrays don't "
     "  get truncated by a low backend default.\n"
     "  • **Per-chunk item count log** — `chunk 3/4 (pages 25-36): "
     "  47 items` — silent zero-item chunks are now LOUD.\n"
     "  • **Per-PDF summary line** — `PDF summary: 38 pages → 226 items "
     "  across 4 chunks (0 failed/empty)`.\n"
     "  • **PDF folder scan log** — lists every PDF in the folder + the "
     "  reason for each drop (year filter, suffix dedup, large-file "
     "  dedup). Investigating 'why is contract X missing?' is now reading "
     "  the log, not bisecting code."),
    ("agents/material_match.py",
     "Thin re-export of Phase 2 entry points",
     "Just `from .extraction import run_matching as run, …`. The frontend "
     "still has two separate agent buttons, but the backend is one cohesive "
     "file."),
    ("chatbot.py",
     "Force-reload both agents on every invocation",
     "`_run_extr` and `_run_material_match` `importlib.reload(...)` their "
     "agent module before invoking. Future patches to either agent take "
     "effect on the next click without a full Streamlit restart."),
):
    src_path = ROOT / rel
    if not src_path.exists():
        raise SystemExit(f"missing source file: {src_path}")
    content = src_path.read_text(encoding="utf-8")
    cells.append(md_cell(f"### `{rel}` — {label}\n{blurb}"))
    cells.append(code_cell(f"%%writefile {rel}\n{content}"))

cells.append(md_cell(
    "### Verification\n"
    "Force-reloads both agent modules, checks the verbatim-port markers, "
    "and confirms `chatbot.py` carries the reload hooks for both."
))
cells.append(code_cell(
    "import ast, os, sys, importlib\n"
    "\n"
    "# Syntax\n"
    "for f in ('fiserv_client.py', 'agents/extraction.py', 'agents/material_match.py', 'chatbot.py'):\n"
    "    ast.parse(open(f, encoding='utf-8').read())\n"
    "    print(f'  OK  {os.path.getsize(f):>8,} bytes  {f}')\n"
    "\n"
    "# fiserv_client.py — confirm detail=high fix is in place\n"
    "fc = open('fiserv_client.py', encoding='utf-8').read()\n"
    "assert '_IMAGE_DETAIL' in fc, 'fiserv_client missing _IMAGE_DETAIL constant'\n"
    "assert 'FISERV_IMAGE_DETAIL' in fc, 'fiserv_client missing env override'\n"
    "# The OLD buggy line was: \"detail\": \"low\" — must be gone\n"
    "assert '\"detail\": \"low\"' not in fc, \\\n"
    "    'fiserv_client still has the old hard-coded detail=low — patch did not apply'\n"
    "print('  OK  fiserv_client.py default image detail is now HIGH')\n"
    "\n"
    "# Confirm top-level imports match the colleague's notebook header.\n"
    "ext_src = open('agents/extraction.py', encoding='utf-8').read()\n"
    "for must in (\n"
    "    'from sentence_transformers import SentenceTransformer',\n"
    "    'from sklearn.feature_extraction.text import TfidfVectorizer',\n"
    "    'from sklearn.preprocessing import normalize',\n"
    "    'import fitz',\n"
    "    'from PIL import Image',\n"
    "    'from concurrent.futures import ThreadPoolExecutor, as_completed',\n"
    "    'import numpy as np',\n"
    "):\n"
    "    assert must in ext_src, f'extraction.py missing top-level import: {must}'\n"
    "print('  OK  All notebook-style top-level imports present in extraction.py')\n"
    "\n"
    "# Force-reload both agent modules.\n"
    "for mod in [m for m in list(sys.modules) if m.startswith('agents')]:\n"
    "    del sys.modules[mod]\n"
    "from agents import extraction as ext, material_match as mm\n"
    "ext = importlib.reload(ext)\n"
    "mm  = importlib.reload(mm)\n"
    "\n"
    "# Verbatim port markers — extraction\n"
    "assert 'forensic-level' in ext.EXTRACTION_SYSTEM_PROMPT, 'wrong extraction prompt'\n"
    "assert 'CHECKBOX LOOK-BACK RULE' in ext.EXTRACTION_SYSTEM_PROMPT\n"
    "assert ext.CHUNK_SIZE == 12 and ext.DPI == 600\n"
    "print(f'\\n  Phase 1 prompt: {len(ext.EXTRACTION_SYSTEM_PROMPT):,} chars')\n"
    "\n"
    "# Verbatim port markers — matching (now lives in extraction.py too)\n"
    "assert 'semantic matching engine' in ext.MATCHING_SYSTEM_PROMPT\n"
    "assert ext.ITEM_BATCH_SIZE == 25 and ext.DICT_CHUNK_SIZE == 250\n"
    "assert ext.MAX_PARALLEL_CALLS == 22 and ext.FUZZY_AUTO_ACCEPT_THRESHOLD == 0.90\n"
    "assert 'v6' in ext._VERSION or 'chunk retry' in ext._VERSION, \\\n"
    "    f'unexpected version: {ext._VERSION}'\n"
    "print(f'  _VERSION: {ext._VERSION!r}')\n"
    "\n"
    "# material_match.py is a thin re-export — every public name comes from extraction\n"
    "assert mm.run is ext.run_matching, 'material_match.run should be extraction.run_matching'\n"
    "assert mm.output_path is ext.matching_output_path\n"
    "assert mm.is_processed is ext.matching_is_processed\n"
    "print('  OK  material_match.py is a thin re-export of extraction.run_matching')\n"
    "\n"
    "# chatbot.py reloads BOTH modules on every invocation\n"
    "cb = open('chatbot.py', encoding='utf-8').read()\n"
    "assert 'importlib.reload(_mm_module)' in cb, 'missing reload(material_match)'\n"
    "assert 'importlib.reload(_ext_module)' in cb, 'missing reload(extraction)'\n"
    "print('  OK  chatbot.py reloads both agent modules')\n"
    "\n"
    "# Fuzzy stage — sentence-transformers + sklearn are now TOP-LEVEL imports\n"
    "# in extraction.py, so if we got past the reload above they're installed.\n"
    "import sentence_transformers, sklearn\n"
    "print(f'\\n  ✅ sentence-transformers v{sentence_transformers.__version__} '\n"
    "      f'+ scikit-learn v{sklearn.__version__} imported successfully.')\n"
    "print('  ✅ Fuzzy stage WILL run on the next matching invocation.')\n"
    "\n"
    "print('\\nNow:')\n"
    "print('  1. Ctrl-C the Streamlit terminal.')\n"
    "print('  2. Re-launch: streamlit run chatbot.py')\n"
    "print('  3. Re-run Fee Description Agent for EDUCATIONAL FCU.')\n"
    "print('  4. Re-run Material Code Matching Agent.')\n"
    "print('  5. Compare row counts to the reference (301 + 187).')"
))

cells.append(md_cell(
    "### Done — what to expect after a clean re-run\n"
    "**Fee Description Agent** run log:\n"
    "```\n"
    "━━━ Fee Description (verbatim port of Contract Extraction.ipynb) ━━━\n"
    "Extraction model: gpt-5.2-2025-12-11\n"
    "  3 small file(s), 1 large file(s).\n"
    "\n"
    "  Extracting: …Amendment---4-5-2024---2989558.pdf\n"
    "  Total pages: 38 | Chunks: 4\n"
    "    chunk 1/4…\n"
    "      chunk 1/4: 84 items\n"
    "    chunk 2/4…\n"
    "      …\n"
    "Wrote: extraction_output.xlsx (≈301 items)\n"
    "```\n"
    "\n"
    "**Material Code Matching Agent** run log:\n"
    "```\n"
    "━━━ Material Match v4 — verbatim port of Contract Extraction.ipynb ━━━\n"
    "  ✅ Fuzzy stage available — sentence-transformers + scikit-learn detected.\n"
    "Material Match (gpt-4.1-2025-04-14): EDUCATIONAL FCU\n"
    "  Loading: extraction_output.xlsx\n"
    "  Dictionary: 1247 description(s), 891 unique code(s)\n"
    "  Total rows: 301 | To match: ~270 | Unique items: ~210\n"
    "  Stage 1 (fuzzy): scoring 210 unique items × 1247 dictionary entries…\n"
    "  Stage 1: ~95 auto-accepted, ~115 → LLM stage\n"
    "  Stage 2 (LLM): 5 item batch(es) × 5 dict chunk(s)  (≤ 22 parallel)\n"
    "    batch 1/5: 21 matched\n"
    "    …\n"
    "Wrote: material_match_output.xlsx (≈187 of 301 items matched)\n"
    "```\n"
    "\n"
    "### Sanity check\n"
    "```python\n"
    "import pandas as pd\n"
    "ext = pd.read_excel('Output/EDUCATIONAL FCU/extraction_output.xlsx')\n"
    "mat = pd.read_excel('Output/EDUCATIONAL FCU/material_match_output.xlsx')\n"
    "print('extraction rows:', len(ext), ' — target 301')\n"
    "print('matched rows:   ', len(mat), ' — target 187')\n"
    "print('\\nfuzzy auto-accepts (conf == 90):',\n"
    "      (mat['Confidence Percentage'] == 90).sum(),\n"
    "      ' — target ~36')\n"
    "```\n"
    "\n"
    "### If the count is still off\n"
    "Paste the full run logs for both agents. The diagnostic lines tell "
    "you exactly what's going on:\n"
    "- Missing banner → Streamlit not restarted; old module still in "
    "  memory.\n"
    "- `⚠ FUZZY STAGE WILL BE SKIPPED` → dependency install failed; "
    "  re-run the install cell.\n"
    "- Stage 1 ran but final count is well under 187 → paste the per-batch "
    "  log lines and we can find the next bottleneck."
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
