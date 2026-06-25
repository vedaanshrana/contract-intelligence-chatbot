"""
Scope Agent — picks the relevant subset of contracts for each downstream agent.

Why this exists
---------------
A client folder can have 30+ amendments accumulated over a decade.  Most of them
don't affect every aspect of the deal — a 2009 amendment that only swaps a
contact person is irrelevant to today's pricing, and an amendment that ONLY
changes the term shouldn't trigger a Volume-Tiers re-extraction.

This agent reads the metadata hierarchy already produces, plus the
contract_summary the Master-Contract agent extracted, and asks a cheap text
model:  "For each of these downstream tasks, which of these contracts should
we actually process?"

Output:  Output/<Client>/scope_report.json — a per-agent allowlist of filenames.
The Load button looks at that file and passes `contracts=[...]` to each agent.

Falls back to "everything" if the LLM call fails or if no metadata exists yet,
so the system never accidentally hides contracts from downstream agents.
"""

import json
import re
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from fiserv_client import make_client

from config import SCOPE_AGENT_API_KEY, SCOPE_AGENT_MODEL

_ADAPTER_DIR = Path(__file__).resolve().parent.parent
_INPUT_DIR   = _ADAPTER_DIR / "Input"
_OUTPUT_DIR  = _ADAPTER_DIR / "Output"


# Downstream agents we make scope decisions for.  Hierarchy + master_contract
# are NOT here — they always run on every PDF (they're how we know what's there).
SCOPED_AGENTS = (
    "extraction",
    "cpi",
    "term_renewal",
    "termination",
    "sla",
    "volume_tiers",
)

_AGENT_QUESTION = {
    "extraction":   "Is this contract likely to contain dollar-valued fee items or service charges that need extracting?",
    "cpi":          "Is this contract likely to contain CPI / annual fee escalation / Consumer Price Index language?",
    "term_renewal": "Does this contract set or modify the TERM length, renewal period, auto-renewal, or expiration?",
    "termination":  "Does this contract set or modify TERMINATION clauses, notice periods, early-termination fees, or survival?",
    "sla":          "Does this contract set or modify SERVICE LEVEL AGREEMENTS — uptime, response/resolution time, or service credits?",
    "volume_tiers": "Does this contract set or modify VOLUME tiers, minimum commitments, tiered pricing, true-ups, or overage charges?",
}

_SYSTEM_PROMPT = (
    "You are a contract-triage assistant. Given metadata for a list of contracts "
    "(filename, type, effective date, and a short summary), decide which ones are "
    "worth processing for a specific downstream task. "
    "Be INCLUSIVE — when in doubt, include the contract. Only exclude when you are "
    "confident the contract does NOT touch the topic at all (e.g. a 2009 amendment "
    "that only renames a contact person is irrelevant to current pricing). "
    "Return STRICT JSON only — no commentary."
)

_USER_TEMPLATE = """\
TASK: {question}

CONTRACTS (numbered):
{contracts_block}

For EACH contract, decide whether to include it. Return a JSON object:

{{
  "included": [<contract numbers to INCLUDE, integers>],
  "reasoning": "<one short paragraph explaining your overall picks>"
}}

Rules:
- Be inclusive — include any contract that plausibly touches the topic.
- The Master Agreement is almost always included.
- Pure administrative amendments (name change, address change, contact swap)
  can be excluded for pricing/CPI/SLA/volume questions but should be kept for
  term/termination since those clauses can hide there.
- Return ONLY the JSON. No prose outside the object.
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_contract_metadata(client_name: str, core: str = "") -> list:
    """Collect per-contract metadata from hierarchy_cache + master_contract output."""
    # 1) Pull every PDF in the input folder so unprocessed contracts still appear.
    folder = (_INPUT_DIR / core / client_name) if core else (_INPUT_DIR / client_name)
    if not folder.exists():
        return []
    pdfs = sorted(
        set(list(folder.glob("*.pdf")) + list(folder.glob("*.PDF"))),
        key=lambda p: p.name,
    )

    # 2) Read hierarchy cache for contract_type / effective_date / parties.
    cache_path = _OUTPUT_DIR / "hierarchy_cache.json"
    hcache: dict = {}
    if cache_path.exists():
        try:
            with open(cache_path, encoding="utf-8") as f:
                full = json.load(f)
            hcache = {
                k.split("/", 1)[1]: v for k, v in full.items()
                if k.startswith(f"{client_name}/")
            }
        except Exception:
            pass

    # 3) Read master_contract output for the contract_summary (richer).
    mc_path = _OUTPUT_DIR / client_name / "master_contract_output.xlsx"
    mc_map: dict = {}
    if mc_path.exists():
        try:
            df = pd.read_excel(str(mc_path))
            for _, row in df.iterrows():
                mc_map[str(row.get("Filename", "") or "")] = (
                    str(row.get("Contract Summary", "") or "").strip()
                )
        except Exception:
            pass

    out = []
    for p in pdfs:
        meta = hcache.get(p.name, {}) or {}
        summary = mc_map.get(p.name, "") or ""
        out.append({
            "filename":       p.name,
            "contract_type":  (meta.get("contract_type") or "").strip(),
            "effective_date": (meta.get("effective_date") or "").strip(),
            "parties":        " / ".join(str(x) for x in (meta.get("parties") or [])[:3]),
            "summary":        summary[:600],   # cap to control prompt size
        })
    return out


def _format_contracts_for_prompt(contracts_meta: list) -> str:
    lines = []
    for i, c in enumerate(contracts_meta, start=1):
        lines.append(
            f"{i}. [{c['contract_type'] or '?'}] {c['filename']}\n"
            f"   Effective: {c['effective_date'] or '?'}  |  Parties: {c['parties'] or '?'}\n"
            f"   Summary: {c['summary'] or '(no summary — master_contract not yet run)'}"
        )
    return "\n\n".join(lines)


def _ask_for_scope(client, model: str, contracts_meta: list,
                   question: str, log: Callable[[str], None]) -> list:
    """Return the LIST OF FILENAMES the LLM picked for this downstream task.
    Falls back to ALL filenames on any failure (safe default)."""
    if not contracts_meta:
        return []
    all_filenames = [c["filename"] for c in contracts_meta]

    user = _USER_TEMPLATE.format(
        question=question,
        contracts_block=_format_contracts_for_prompt(contracts_meta),
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user},
            ],
            temperature=0.0,
            max_tokens=600,
        )
        text = resp.choices[0].message.content.strip()
        # Strip markdown code fences if present
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
        parsed = json.loads(text)
        included = parsed.get("included", [])
        if not isinstance(included, list):
            raise ValueError("included must be a list")
        # Map numbers → filenames (clamp out-of-range entries)
        picked = []
        for n in included:
            try:
                idx = int(n) - 1
                if 0 <= idx < len(all_filenames):
                    picked.append(all_filenames[idx])
            except Exception:
                continue
        if not picked:
            log(f"    ⚠ Scope returned empty set — defaulting to ALL contracts")
            return all_filenames
        return picked
    except Exception as e:
        log(f"    ⚠ Scope LLM failed ({e}) — defaulting to ALL contracts")
        return all_filenames


# ── Top-level entry point ────────────────────────────────────────────────────

def run(
    client_name: str,
    api_key: str = "",
    model: str = "",
    progress_callback: Optional[Callable[[str], None]] = None,
    core: str = "",
) -> dict:
    """
    Decide a per-agent contract scope for `client_name` and write scope_report.json.

    Returns {status, client, report_path, scopes: {agent → [filenames]}}.
    """
    log     = progress_callback or (lambda m: None)
    api_key = api_key or SCOPE_AGENT_API_KEY
    model   = model   or SCOPE_AGENT_MODEL

    meta = _load_contract_metadata(client_name, core=core)
    if not meta:
        return {"status": "no_contracts", "client": client_name}

    log(f"Scope Agent: {len(meta)} contracts → deciding scope for {len(SCOPED_AGENTS)} agents")

    client = make_client(api_key)
    scopes: dict = {}
    for agent in SCOPED_AGENTS:
        log(f"  • {agent}…")
        scopes[agent] = _ask_for_scope(client, model, meta, _AGENT_QUESTION[agent], log)
        log(f"    → {len(scopes[agent])} of {len(meta)} contracts")

    # Persist alongside the other outputs so the chatbot can show + use it.
    report = {
        "client":    client_name,
        "agents":    list(SCOPED_AGENTS),
        "all_files": [c["filename"] for c in meta],
        "scopes":    scopes,
    }
    out_dir = _OUTPUT_DIR / client_name
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "scope_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    log(f"Wrote: {report_path.name}")

    return {
        "status":      "complete",
        "client":      client_name,
        "report_path": str(report_path),
        "scopes":      scopes,
    }


def is_processed(client_name: str) -> bool:
    p = _OUTPUT_DIR / client_name / "scope_report.json"
    return p.exists() and p.stat().st_size > 200


def load_scope(client_name: str, agent_name: str) -> Optional[list]:
    """Return the per-agent allowlist of filenames, or None if no report exists.
    Callers should treat None as 'process every contract' (the default)."""
    p = _OUTPUT_DIR / client_name / "scope_report.json"
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            report = json.load(f)
        scopes = report.get("scopes", {})
        return scopes.get(agent_name)
    except Exception:
        return None


def output_path(client_name: str) -> Path:
    return _OUTPUT_DIR / client_name / "scope_report.json"
