"""
Backend-agnostic orchestration layer for the Contract Intelligence engine.

This module contains everything the old ``chatbot.py`` (Streamlit) did to drive
the agents, build chat context, and resolve citations — but with **no UI
dependency**. The FastAPI layer (``server.py``) calls into here; so could a
notebook or a test. Nothing in here imports Streamlit.

Design notes
------------
* Agent execution is serialized by ``RUN_LOCK``. ``run_metrics`` keeps a single
  process-global call tape and brackets each agent with snapshot()/finalize();
  if two agents ran concurrently their token counts would bleed together. The
  lock keeps per-agent metrics correct. LLM calls are the bottleneck and are
  rate-limited anyway, so serializing costs little.
* Output files are keyed by client name only (``Output/<client>/``), exactly as
  the original pipeline wrote them — Core only affects *input* (PDF + dictionary
  discovery). This preserves on-disk compatibility with existing runs.
"""
from __future__ import annotations

import importlib
import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

import config
import run_metrics
import app_settings
from config import (
    INPUT_DIR, OUTPUT_DIR,
    EXTRACTION_API_KEY, CPI_API_KEY, CPI_MODEL,
    MASTER_CONTRACT_API_KEY,
    SCOPE_AGENT_API_KEY,
    default_dictionary_for, client_input_dir,
)

try:
    from config import HIERARCHY_API_KEY
except ImportError:        # half-applied patch fallback (mirrors chatbot.py)
    HIERARCHY_API_KEY = EXTRACTION_API_KEY

# Material Validation — added in the Snowflake invoice patch.
try:
    from config import VALIDATION_API_KEY, VALIDATION_MODEL          # type: ignore
except ImportError:
    VALIDATION_API_KEY = EXTRACTION_API_KEY
    VALIDATION_MODEL = "gpt-4.1-2025-04-14"

# MNR Template — added in the SNOWFLAKE/MNR patch. Same fallback chain
# agents/mnr_template.py uses so a half-applied patch still loads.
try:
    from config import MNR_API_KEY                                   # type: ignore
except ImportError:
    MNR_API_KEY = EXTRACTION_API_KEY
try:
    from config import MNR_MODEL                                     # type: ignore
except ImportError:
    MNR_MODEL = "gpt-5.2-2025-12-11"

# Serializes agent execution so run_metrics' shared tape stays per-agent-correct.
RUN_LOCK = threading.Lock()

# Chatbot-answer feedback reports → backend/Feedbacks/feedback.xlsx. A separate
# lock (not RUN_LOCK) so submitting feedback never contends with agent runs.
FEEDBACK_DIR = config.BASE_DIR / "Feedbacks"
_FEEDBACK_LOCK = threading.Lock()


# ── Agent catalogue ───────────────────────────────────────────────────────────
# The 9 user-facing agents, in pipeline order. Each maps 1:1 to a backend module.
FRONTEND_AGENTS: tuple[tuple[str, str], ...] = (
    ("contract_hierarchy", "Hierarchy Agent"),
    ("contract_scope",     "Engagement Overview Agent"),
    ("product_module",     "Product Module Agent"),
    ("fee_digitization",   "Fee Description Agent"),
    ("material_match",     "Material Code Matching Agent"),
    ("material_validation","Material Validation Agent"),
    ("cpi_terms",          "CPI Terms Agent"),
    ("termination_clause", "Termination Clause Agent"),
    ("mnr_template",       "MNR Template Agent"),
)

# The full load pipeline (what the old "Load New Clients" button ran), in order.
# `internal` agents still run + feed chat context but aren't headline cards.
# key, display, internal
PIPELINE: tuple[tuple[str, str, bool], ...] = (
    ("contract_hierarchy", "Hierarchy Agent",                False),
    ("contract_scope",     "Engagement Overview Agent",      False),
    ("product_module",     "Product Module Agent",           False),
    ("scope_agent",        "Scope Triage",                   True),
    ("fee_digitization",   "Fee Description Agent",           False),
    ("material_match",     "Material Code Matching Agent",    False),
    ("material_validation","Material Validation Agent",       False),
    ("cpi_terms",          "CPI Terms Agent",                 False),
    ("term_renewal",       "Term & Renewal",                 True),
    ("termination_clause", "Termination Clause Agent",        False),
    ("sla",                "SLA & Credits",                  True),
    ("volume_tiers",       "Volume Tiers",                    True),
    ("mnr_template",       "MNR Template Agent",              False),
)

# Map a frontend/pipeline key → the backend clause-agent module name.
_CLAUSE_KEYS = {"term_renewal": "term_renewal", "termination_clause": "termination",
                "sla": "sla", "volume_tiers": "volume_tiers"}


# ── Cores & clients (input discovery) ──────────────────────────────────────────

def list_cores() -> list[str]:
    if not INPUT_DIR.exists():
        return []
    return sorted(p.name for p in INPUT_DIR.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


def list_clients(core: str = "") -> list[str]:
    base = (INPUT_DIR / core) if core else INPUT_DIR
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


def list_pdfs(client: str, core: str = "") -> list[dict]:
    """Return [{name, label}] for every PDF in this client's input folder."""
    cdir = client_input_dir(client, core)
    if not cdir.exists():
        return []
    pdfs = sorted(set(list(cdir.glob("*.pdf")) + list(cdir.glob("*.PDF"))),
                  key=lambda p: p.name)
    return [{"name": p.name, "label": f"{client} — {p.name}"} for p in pdfs]


def resolve_pdf_path(client: str, core: str, name: str) -> Optional[Path]:
    """Safely resolve a PDF filename within a client's input folder (no escape)."""
    cdir = client_input_dir(client, core)
    if not cdir.exists():
        return None
    candidate = (cdir / name)
    try:
        candidate = candidate.resolve()
        if cdir.resolve() not in candidate.parents:
            return None
    except Exception:
        return None
    return candidate if candidate.exists() else None


# ── Agent status ────────────────────────────────────────────────────────────--
# The agent output filenames under Output/<client>/. Kept here so status checks
# and output reads are pure path math — importing the agent modules (e.g.
# extraction → sentence-transformers, ~hundreds of MB) is reserved for when an
# agent actually RUNS. These mirror each module's output_path()/is_processed().
_OUTPUT_FILES: dict[str, str] = {
    "contract_hierarchy": "contracts_hierarchy.xlsx",
    "contract_scope":     "engagement_overview_output.xlsx",
    "product_module":     "product_hierarchy_output.xlsx",
    "fee_digitization":   "extraction_output.xlsx",
    "material_match":     "material_match_output.xlsx",
    "material_validation":"validated_material_output.xlsx",
    "cpi_terms":          "cpi_output.xlsx",
    "termination_clause": "termination_output.xlsx",
    "scope_agent":        "scope_report.json",
    "term_renewal":       "term_renewal_output.xlsx",
    "sla":                "sla_output.xlsx",
    "volume_tiers":       "volume_tiers_output.xlsx",
    "mnr_template":       "mnr_output.xlsx",
}


def _frontend_is_processed(key: str, client: str) -> bool:
    """File-based equivalent of each agent module's is_processed() — fast, no
    heavy imports. Matches the modules' own thresholds (size guard on
    engagement / material_validation; non-empty Product column on
    product_module)."""
    fname = _OUTPUT_FILES.get(key)
    if not fname:
        return False
    p = OUTPUT_DIR / client / fname
    try:
        if key == "contract_scope":
            if p.exists() and p.stat().st_size > 1_000:
                return True
            legacy = OUTPUT_DIR / client / "master_contract_output.xlsx"
            return legacy.exists() and legacy.stat().st_size > 1_000
        if key == "product_module":
            if not (p.exists() and p.stat().st_size > 1_000):
                return False
            df = pd.read_excel(str(p))
            if df.empty or "Product" not in df.columns:
                return False
            prod = df["Product"].astype(str).str.strip()
            return bool(((prod != "") & (prod.str.lower() != "nan")).any())
        if key == "material_validation":
            # Mirrors agents/material_validation.is_processed — size guard rejects
            # the tiny no-Snowflake stub so a skipped run doesn't read as done.
            return p.exists() and p.stat().st_size > 1_000
        return p.exists()
    except Exception:
        return False


def frontend_done_count(client: str) -> int:
    return sum(1 for k, _ in FRONTEND_AGENTS if _frontend_is_processed(k, client))


def count_contracts(client: str) -> int:
    """Number of contracts discovered for a client. Prefers the hierarchy cache;
    falls back to row count of contracts_hierarchy.xlsx (robust when the global
    cache file is absent)."""
    cache = OUTPUT_DIR / "hierarchy_cache.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            n = sum(1 for k in data if str(k).startswith(f"{client}/"))
            if n:
                return n
        except Exception:
            pass
    xlsx = OUTPUT_DIR / client / "contracts_hierarchy.xlsx"
    if xlsx.exists():
        try:
            return len(pd.read_excel(str(xlsx)))
        except Exception:
            return 0
    return 0


def client_status(client: str) -> dict:
    """Per-agent done flags + summary counts for one client."""
    agents = {k: _frontend_is_processed(k, client) for k, _ in FRONTEND_AGENTS}
    done = sum(1 for v in agents.values() if v)
    total = len(FRONTEND_AGENTS)
    return {
        "client": client,
        "agents": agents,
        "agentsDone": done,
        "agentsTotal": total,
        "contracts": count_contracts(client),
        "state": "done" if done == total else ("partial" if done else "not_run"),
    }


# ── Output readers ─────────────────────────────────────────────────────────────

def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """JSON-safe records: NaN/NaT → None, numpy scalars → python."""
    df = df.where(pd.notna(df), None)
    records = df.to_dict(orient="records")
    out: list[dict] = []
    for row in records:
        clean: dict = {}
        for k, v in row.items():
            if isinstance(v, float) and math.isnan(v):
                v = None
            elif hasattr(v, "item"):          # numpy scalar
                try:
                    v = v.item()
                except Exception:
                    v = str(v)
            clean[str(k)] = v
        out.append(clean)
    return out


def output_path_for(key: str, client: str) -> Optional[Path]:
    """Path to an agent's xlsx output. Pure path math (see _OUTPUT_FILES)."""
    fname = _OUTPUT_FILES.get(key)
    if not fname:
        return None
    # contract_scope falls back to the legacy master_contract_output.xlsx so
    # older runs still render in the Outputs view.
    if key == "contract_scope":
        primary = OUTPUT_DIR / client / fname
        if primary.exists():
            return primary
        legacy = OUTPUT_DIR / client / "master_contract_output.xlsx"
        return legacy if legacy.exists() else primary
    return OUTPUT_DIR / client / fname


def hierarchy_html_path(client: str) -> Optional[Path]:
    """Path to the interactive Plotly hierarchy graph the Hierarchy agent writes
    (Output/<client>/contracts_hierarchy.html), or None when it hasn't been
    generated yet. Self-contained HTML — Plotly JS is embedded."""
    p = OUTPUT_DIR / client / "contracts_hierarchy.html"
    return p if p.exists() else None


def read_output(key: str, client: str) -> dict:
    """Return {columns, rows, path, exists} for an agent's xlsx output."""
    p = output_path_for(key, client)
    if not p or not Path(p).exists():
        return {"columns": [], "rows": [], "exists": False, "path": str(p) if p else ""}
    try:
        df = pd.read_excel(str(p))
    except Exception as e:
        return {"columns": [], "rows": [], "exists": True, "path": str(p),
                "error": str(e)}
    return {
        "columns": [str(c) for c in df.columns],
        "rows": _df_to_records(df),
        "exists": True,
        "path": str(p),
    }


# ── Single-agent execution (metered + serialized) ───────────────────────────────

def _scope_for(client: str, agent_name: str):
    from agents.scope_agent import load_scope
    try:
        return load_scope(client, agent_name)
    except Exception:
        return None


def _resolve_dict_path(core: str) -> Optional[Path]:
    override = app_settings.get("dict_path")
    if override:
        return Path(override)
    d = default_dictionary_for(core)
    return d if d and Path(d).exists() else None


def run_one_agent(key: str, client: str, core: str,
                  log: Optional[Callable[[str], None]] = None) -> dict:
    """Run a single agent (always runs, ignoring cache) and return its result
    dict. Serialized + metered so token attribution stays correct."""
    log = log or (lambda m: None)
    s = app_settings.get_all()
    display = dict((k, d) for k, d, _ in PIPELINE).get(key, key)

    with RUN_LOCK:
        mark = run_metrics.snapshot()
        t0 = time.perf_counter()
        ok = False
        fallback_model = ""
        metric_agent = key
        metric_display = display
        try:
            result = _dispatch_agent(key, client, core, s, log)
            ok = (result or {}).get("status", "complete") in ("complete", "ok", None) \
                 or "rows" in (result or {})
            fallback_model, metric_agent, metric_display = _metric_meta(key, s)
            return result or {"status": "complete"}
        except Exception as e:
            log(f"❌ {display} failed: {e}")
            return {"status": "error", "error": str(e), "agent": key}
        finally:
            try:
                run_metrics.finalize(client, metric_agent, metric_display, t0, mark,
                                     fallback_model=fallback_model,
                                     status="complete" if ok else "error")
            except Exception:
                pass


def _metric_meta(key: str, s: dict) -> tuple[str, str, str]:
    """(fallback_model, metric_agent_key, metric_display) used by run_metrics."""
    table = {
        "contract_hierarchy": (s["hier_model"], "hierarchy", "Hierarchy Agent"),
        "contract_scope":     (s["engagement_model"], "engagement_overview", "Engagement Overview Agent"),
        "product_module":     (s["engagement_model"], "product_module", "Product Module Agent"),
        "scope_agent":        (s["scope_model"], "scope_agent", "Scope Triage (internal)"),
        "fee_digitization":   (s["extr_model"], "extraction", "Fee Description Agent"),
        "material_match":     (s["match_model"], "material_match", "Material Code Matching Agent"),
        "material_validation":(VALIDATION_MODEL, "material_validation", "Material Validation Agent"),
        "cpi_terms":          (s["cpi_model"], "cpi", "CPI Terms Agent"),
        "term_renewal":       ("", "term_renewal", "Term & Renewal (internal)"),
        "termination_clause": ("", "termination", "Termination Clause Agent"),
        "sla":                ("", "sla", "SLA & Credits (internal)"),
        "volume_tiers":       ("", "volume_tiers", "Volume Tiers (internal)"),
        "mnr_template":       (MNR_MODEL, "mnr_template", "MNR Template Agent"),
    }
    return table.get(key, ("", key, key))


def _dispatch_agent(key: str, client: str, core: str, s: dict,
                    log: Callable[[str], None]) -> dict:
    """Call the correct backend module for one agent key. Returns its result."""
    core_u = (core or "").upper()
    min_year = int(s.get("min_year", 2022) or 2022)

    if key == "contract_hierarchy":
        from agents.hierarchy import run as hier_run
        return hier_run(client, api_key=HIERARCHY_API_KEY,
                        hierarchy_model=s["hier_model"],
                        progress_callback=log, core=core)

    if key == "contract_scope":
        from agents.engagement_overview import run as eo_run
        return eo_run(client, api_key=MASTER_CONTRACT_API_KEY,
                      model=s["engagement_model"], progress_callback=log, core=core)

    if key == "product_module":
        from agents.product_module import run as pm_run
        return pm_run(client, api_key=MASTER_CONTRACT_API_KEY,
                      model=s["engagement_model"], progress_callback=log, core=core)

    if key == "scope_agent":
        from agents.scope_agent import run as scope_run
        return scope_run(client, api_key=SCOPE_AGENT_API_KEY,
                         model=s["scope_model"], progress_callback=log, core=core)

    if key == "fee_digitization":
        if core_u == "DNA":
            mod = importlib.reload(importlib.import_module("agents.dna_extraction"))
        else:
            mod = importlib.reload(importlib.import_module("agents.extraction"))
        return mod.run(client, api_key=EXTRACTION_API_KEY,
                       extraction_model=s["extr_model"], progress_callback=log,
                       contracts=_scope_for(client, "extraction"),
                       core=core, min_year=min_year)

    if key == "material_match":
        dict_p = _resolve_dict_path(core)
        if dict_p:
            log(f"Using dictionary: {Path(dict_p).name}")
        else:
            log("No material-code dictionary set — skipping matching")
        if core_u == "DNA":
            mod = importlib.reload(importlib.import_module("agents.dna_extraction"))
            return mod.run_matching(client, api_key=EXTRACTION_API_KEY,
                                    matching_model=s["match_model"],
                                    dictionary_path=dict_p, progress_callback=log,
                                    core=core, min_year=min_year)
        mod = importlib.reload(importlib.import_module("agents.material_match"))
        return mod.run(client, api_key=EXTRACTION_API_KEY,
                       matching_model=s["match_model"], dictionary_path=dict_p,
                       progress_callback=log, core=core, min_year=min_year)

    if key == "cpi_terms":
        from agents.cpi import run_full as cpi_run_full
        return cpi_run_full(client, api_key=CPI_API_KEY, model=s["cpi_model"],
                            progress_callback=log,
                            contracts=_scope_for(client, "cpi"), core=core)

    if key == "material_validation":
        # Re-scores matched material codes against historical Snowflake invoice
        # data. Self-skips (writes no file) when Snowflake is unreachable, so
        # it's safe to call unconditionally during Load.
        from agents.material_validation import run as mv_run
        return mv_run(client, api_key=VALIDATION_API_KEY,
                      progress_callback=log, core=core)

    if key == "mnr_template":
        # Forensic extraction + Portico material matching → SAP-ready MNR draft.
        # Intentionally no min_year — MNR runs on the latest master agreement
        # regardless of when it was signed.
        from agents.mnr_template import run as mnr_run
        dict_p = _resolve_dict_path(core)
        return mnr_run(client, api_key=MNR_API_KEY, model=MNR_MODEL,
                       progress_callback=log,
                       contracts=_scope_for(client, "mnr_template"),
                       core=core, dictionary_path=dict_p)

    if key in _CLAUSE_KEYS:
        agent_name = _CLAUSE_KEYS[key]
        mod = importlib.import_module(f"agents.{agent_name}")
        return mod.run(client, progress_callback=log,
                       contracts=_scope_for(client, agent_name), core=core)

    raise ValueError(f"unknown agent key: {key}")


# ── Full pipeline as an event stream ───────────────────────────────────────────

def pipeline_events(client: str, core: str, force: bool = False):
    """Generator yielding pipeline progress events for one client.

    Mirrors the old "Load New Clients" trigger: each agent is skipped when its
    output already exists (unless ``force``). Events:
      {type:'pipeline_start', client, total}
      {type:'agent_start',  key, display, internal, index, total}
      {type:'log',          key, message}
      {type:'agent_done',   key, display, status, summary, index, total, agentsDone}
      {type:'pipeline_done', client, agentsDone, total, elapsedMs}
      {type:'error', message}
    """
    steps = list(PIPELINE)
    total = len(steps)
    t_start = time.perf_counter()
    yield {"type": "pipeline_start", "client": client, "total": total}

    done_count = 0
    for idx, (key, display, internal) in enumerate(steps):
        already = _frontend_is_processed(key, client)
        yield {"type": "agent_start", "key": key, "display": display,
               "internal": internal, "index": idx, "total": total}
        if already and not force:
            done_count += 1
            yield {"type": "agent_done", "key": key, "display": display,
                   "status": "cached", "summary": "already processed",
                   "index": idx, "total": total, "agentsDone": done_count}
            continue

        logs: list[str] = []

        def _log(msg: str, _logs=logs):
            _logs.append(str(msg))

        result = run_one_agent(key, client, core, log=_log)
        for line in logs[-12:]:
            yield {"type": "log", "key": key, "message": line}

        status = (result or {}).get("status", "complete")
        summary = _summarize_result(key, result)
        # "complete" / "cached" count as done; everything else doesn't.
        if status in ("complete", "cached", "ok") or (result and "rows" in result and status != "error"):
            done_count += 1
            out_status = "complete"
        else:
            out_status = status or "error"
        yield {"type": "agent_done", "key": key, "display": display,
               "status": out_status, "summary": summary,
               "index": idx, "total": total, "agentsDone": done_count}

    elapsed_ms = int((time.perf_counter() - t_start) * 1000)
    yield {"type": "pipeline_done", "client": client,
           "agentsDone": done_count, "total": total, "elapsedMs": elapsed_ms}


# Human-readable reasons for the "ran but produced nothing" statuses, so the UI
# explains WHY an agent made no output instead of showing a bare "0 rows".
_SKIP_REASONS: dict[str, str] = {
    "no_pdfs":            "no input PDFs for this client",
    "no_master_agreement":"no master agreement found — run Hierarchy / Engagement Overview first",
    "no_master":          "no Core dictionary found",
    "no_catalog":         "'Frequently Used Material Codes.xlsx' not found",
    "no_matching":        "Material Code Matching must run first",
    "no_history":         "no SAP invoice history matched this client",
    "no_items":           "nothing to process",
    "no_snowflake":       "SAP invoice data (Snowflake) unavailable",
}


def _summarize_result(key: str, result: Optional[dict]) -> str:
    if not result:
        return ""
    status = result.get("status")
    if status == "error":
        return result.get("error", "error")
    if status in _SKIP_REASONS:
        return f"skipped — {_SKIP_REASONS[status]}"
    n = result.get("rows")
    if n is not None:
        return f"{n} rows"
    if "scopes" in result:
        try:
            return f"{sum(len(v) for v in result['scopes'].values())} assignments"
        except Exception:
            return "complete"
    return result.get("status", "complete")


# ── Portfolio KPIs (dashboard) ──────────────────────────────────────────────────

def portfolio_kpis(clients: list[str]) -> dict:
    """Aggregate headline metrics across clients (ported from chatbot._portfolio_kpis)."""
    contracts = items = matched = 0
    value = 0.0
    agents_done = agents_total = 0
    active = pending = expired = 0
    for c in clients:
        contracts += count_contracts(c)
        agents_total += len(FRONTEND_AGENTS)
        agents_done += frontend_done_count(c)
        # Total codes = every line item description extracted from the contracts
        # (one row per item in extraction_output.xlsx).
        ext = OUTPUT_DIR / c / "extraction_output.xlsx"
        if ext.exists():
            try:
                df = pd.read_excel(str(ext))
                items += len(df)
                price_col = "Cleaned Price" if "Cleaned Price" in df.columns else "Price"
                if price_col in df.columns:
                    nums = pd.to_numeric(
                        df[price_col].astype(str).str.replace(r"[^0-9.\-]", "", regex=True),
                        errors="coerce")
                    value += float(nums.sum(skipna=True) or 0)
            except Exception:
                pass
        # Matched = rows in the validated material output (the system of record
        # for confirmed material-code matches). The size guard rejects the tiny
        # no-Snowflake stub so a skipped validation run doesn't read as matches.
        val = OUTPUT_DIR / c / "validated_material_output.xlsx"
        if val.exists() and val.stat().st_size > 1_000:
            try:
                matched += len(pd.read_excel(str(val)))
            except Exception:
                pass
        # Lifecycle (active/pending/expired) from the hierarchy status map.
        try:
            from context_builder import _get_contract_status
            for _, info in _get_contract_status(c).items():
                st = info.get("status")
                if st == "ACTIVE":
                    active += 1
                elif st in ("ROOT-PARTIAL",):
                    pending += 1
                elif st in ("SUPERSEDED", "ORPHAN"):
                    expired += 1
        except Exception:
            pass
    pct = int(round(100 * agents_done / agents_total)) if agents_total else 0
    return {
        "clients": len(clients),
        "contracts": contracts,
        "items": items,
        "matched": matched,
        "unmatched": max(0, items - matched),
        "value": value,
        "pipelinePct": pct,
        "lifecycle": {"active": active, "pending": pending, "expired": expired},
    }


def client_metrics(client: str) -> dict:
    """run_metrics.json history + latest-by-agent for one client."""
    runs = run_metrics.load_runs(client)
    latest = run_metrics.latest_by_agent(client)
    total_in = sum(r.get("input_tokens", 0) for r in runs)
    total_out = sum(r.get("output_tokens", 0) for r in runs)
    total_runtime = sum(r.get("runtime_s", 0) for r in runs)
    return {
        "client": client,
        "runs": runs,
        "latestByAgent": latest,
        "totals": {
            "inputTokens": total_in,
            "outputTokens": total_out,
            "runtimeS": round(total_runtime, 2),
            "runCount": len(runs),
        },
    }


# ── Citations (ported from chatbot.py) ──────────────────────────────────────────

def find_cited_pdfs(message_text: str, pdf_entries: list[dict]) -> list[dict]:
    """Identify which PDFs (from list_pdfs entries with client/name) are cited in
    an assistant message. Returns [{client, name, label, page}]."""
    if not message_text or not pdf_entries:
        return []
    sources_match = re.search(
        r'(?im)^\s*(?:#+\s*|\*+\s*)?sources?\s*:?\s*(?:\*+\s*)?$', message_text)
    search_in = message_text[sources_match.end():] if sources_match else message_text
    search_low = search_in.lower()

    found: list[dict] = []
    seen: set = set()
    for entry in pdf_entries:
        name = entry["name"]
        stem = name.rsplit(".", 1)[0]
        if name in seen:
            continue
        if name.lower() in search_low or stem.lower() in search_low:
            page = _first_page_for(search_in, name, stem)
            found.append({"client": entry.get("client", ""), "name": name,
                          "label": entry.get("label", name), "page": page})
            seen.add(name)
    return found


def _first_page_for(text: str, name: str, stem: str) -> Optional[int]:
    """Best-effort: the smallest p.N appearing soon after the filename mention."""
    idx = text.lower().find(name.lower())
    if idx < 0:
        idx = text.lower().find(stem.lower())
    if idx < 0:
        return None
    window = text[idx: idx + 200]
    pages = [int(m.group(1)) for m in re.finditer(r'p\.\s*(\d+)', window)]
    return min(pages) if pages else None


def find_cited_invoices(message_text: str) -> list[dict]:
    """SAP invoices referenced in a message → [{doc, url}]. Mirrors chatbot.py."""
    if not message_text:
        return []
    try:
        import snowflake_invoice as _sf
        registry = _sf.get_last_invoice_links()
    except Exception:
        registry = {}
    found: list[dict] = []
    seen: set = set()
    low = message_text.lower()
    for doc, url in (registry or {}).items():
        if not doc or doc in seen:
            continue
        if doc.lower() in low or (url and url.lower() in low):
            found.append({"doc": doc, "url": url})
            seen.add(doc)
    for m in re.finditer(r'https?://\S+', message_text):
        url = m.group(0).rstrip(').,;"\'')
        if url in {f["url"] for f in found}:
            continue
        ctx_start = max(0, m.start() - 60)
        if "[invoice]" in message_text[ctx_start:m.start()].lower():
            if f"link:{url}" not in seen:
                found.append({"doc": "(invoice)", "url": url})
                seen.add(f"link:{url}")
    return found


# ── Chat ────────────────────────────────────────────────────────────────────────

def build_chat_reply(focus: list[str], messages: list[dict],
                     use_snowflake: bool = True) -> dict:
    """Run one chat turn. ``messages`` = [{role, content}] (full history, the
    last entry being the new user question). Returns {reply, citations, invoices}.
    """
    from chat_engine import ChatEngine
    from context_builder import build_multi_client_context
    from config import CHAT_API_KEY

    s = app_settings.get_all()
    ctx = build_multi_client_context(focus)

    last_user = next((m["content"] for m in reversed(messages)
                      if m.get("role") == "user"), "")

    invoice_ctx = ""
    if use_snowflake:
        try:
            import snowflake_invoice as _sf
            if _sf.is_invoice_query(last_user):
                invoice_ctx = _sf.build_invoice_context(focus, last_user)
        except Exception as e:
            invoice_ctx = (
                "=== SAP INVOICE DATA ===\n"
                f"[INVOICE LOOKUP ERROR] {e}\n"
                "Do NOT paraphrase this as a generic 'SQL error' or 'data pull "
                "issue' — quote the error verbatim so the user can act on it, "
                "then answer the rest from contract data if you can.")

    engine = ChatEngine(api_key=CHAT_API_KEY, model=s["chat_model"])
    system_prompt = engine.build_system_prompt(
        client_name=", ".join(focus),
        kb_context=ctx.get("kb", ""),
        hierarchy_context=ctx.get("hierarchy", ""),
        master_contract_context=ctx.get("master_contract", ""),
        product_hierarchy_context=ctx.get("product_hierarchy", ""),
        extraction_context=ctx.get("extraction", ""),
        cpi_context=ctx.get("cpi", ""),
        clauses_context=ctx.get("clauses", ""),
        invoice_context=invoice_ctx,
    )
    history = [{"role": m["role"], "content": m["content"]}
               for m in messages if m.get("role") in ("user", "assistant")]
    reply = engine.chat(history, system_prompt)

    # Resolve citations against every PDF in focus.
    pdf_entries: list[dict] = []
    # Core is needed to find input PDFs; discover it from the client's home core.
    for c in focus:
        core = _core_for_client(c)
        for e in list_pdfs(c, core):
            pdf_entries.append({**e, "client": c, "core": core})

    citations = find_cited_pdfs(reply, pdf_entries)
    # carry core through for the viewer
    core_by_client = {e["client"]: e.get("core", "") for e in pdf_entries}
    for cit in citations:
        cit["core"] = core_by_client.get(cit["client"], "")
    invoices = find_cited_invoices(reply)
    return {"reply": reply, "citations": citations, "invoices": invoices}


def _core_for_client(client: str) -> str:
    """Find which Core a client folder lives under (first match wins)."""
    for core in list_cores():
        if (INPUT_DIR / core / client).is_dir():
            return core
    return ""


# ── Feedback (chatbot answer reports) ───────────────────────────────────────────

def save_feedback(payload: dict) -> dict:
    """Append one chatbot-answer feedback report to backend/Feedbacks/feedback.xlsx.

    Creates the folder + workbook on first use. Each row captures the user's
    title / description / category plus the question, answer, and scope context
    so the team can triage wrong answers later. Coerces every value to str so a
    stray cell can't break the write; serialized by _FEEDBACK_LOCK.
    """
    import datetime

    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    path = FEEDBACK_DIR / "feedback.xlsx"

    clients = payload.get("clients")
    if isinstance(clients, (list, tuple)):
        clients = ", ".join(str(c) for c in clients)

    row = {
        "timestamp":   datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "category":    str(payload.get("category", "") or ""),
        "title":       str(payload.get("title", "") or ""),
        "description": str(payload.get("description", "") or ""),
        "question":    str(payload.get("question", "") or ""),
        "answer":      str(payload.get("answer", "") or ""),
        "core":        str(payload.get("core", "") or ""),
        "clients":     str(clients or ""),
        "chat_model":  str(payload.get("chat_model", "") or ""),
        "user_name":   str(payload.get("user_name", "") or ""),
        "user_email":  str(payload.get("user_email", "") or ""),
    }

    with _FEEDBACK_LOCK:
        if path.exists():
            try:
                df = pd.read_excel(str(path))
                df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            except Exception:
                df = pd.DataFrame([row])
        else:
            df = pd.DataFrame([row])
        df.to_excel(str(path), index=False)
        count = len(df)

    return {"saved": True, "path": str(path), "count": int(count)}
