"""
Contract Intelligence Chatbot — Fiserv FI Billing
Main Streamlit application.

Run:  streamlit run chatbot.py
"""

import json
import re
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

import run_metrics
from chat_engine import ChatEngine
from config import (
    INPUT_DIR, OUTPUT_DIR,
    CHAT_API_KEY, EXTRACTION_API_KEY, CPI_API_KEY, CPI_MODEL,
    MASTER_CONTRACT_API_KEY, MASTER_CONTRACT_MODEL,
    SCOPE_AGENT_API_KEY, SCOPE_AGENT_MODEL,
    VALIDATION_API_KEY, VALIDATION_MODEL,
    default_dictionary_for,
)

# HIERARCHY_API_KEY was added in the gpt-5.2 / hierarchy-rewrite patch.
# Some VDI deployments end up with the new chatbot.py before the new config.py
# (Streamlit hot-reloads chatbot but caches config). Fall back gracefully so a
# half-applied patch doesn't crash the chat UI — every other agent's key is
# already a fallback chain rooted in EXTRACTION_API_KEY anyway.
try:
    from config import HIERARCHY_API_KEY
except ImportError:
    HIERARCHY_API_KEY = EXTRACTION_API_KEY
from context_builder import build_multi_client_context
import ui

# ── Page configuration ────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Contract Intelligence | Fiserv",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

ui.inject_global_css()

# ── Session state ─────────────────────────────────────────────────────────────
_DEFAULTS: dict = {
    "selected_core":    "",   # e.g. "PORTICO"
    "selected_clients": [],   # clients picked in the sidebar (loaded)
    "focus_clients":    [],   # clients currently in conversation focus (subset of selected)
    "ctx_clients":      [],   # clients the current ctx was built for
    "messages":         [],
    "ctx":              {},
    "chat_model":       "gpt-4.1-2025-04-14",
    "hier_model":       "gpt-5.2",
    "extr_model":       "gpt-5.2-2025-12-11",
    "match_model":      "gpt-4.1-2025-04-14",
    "dict_path":        "",
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ── Helpers ───────────────────────────────────────────────────────────────────

def list_cores() -> list[str]:
    """Top-level folders under Input/ (e.g. PORTICO, DNA)."""
    if not INPUT_DIR.exists():
        return []
    return sorted(
        p.name for p in INPUT_DIR.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def list_clients(core: str = "") -> list[str]:
    """Client folders. If `core` is supplied, scope to Input/<core>/."""
    base = (INPUT_DIR / core) if core else INPUT_DIR
    if not base.exists():
        return []
    return sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def _client_pdf_dir(client_name: str) -> Path:
    """Resolve the PDF folder for a client under the currently selected Core."""
    from config import client_input_dir
    return client_input_dir(client_name, st.session_state.get("selected_core", ""))


# Order matters — drives sidebar status, Load button, and Overview tab order.
# Hierarchy + master_contract run on ALL contracts (they're how we know what's there).
# Scope_agent runs once after them.  The remaining six are SCOPED — each may run
# on only a subset of contracts per the scope agent's recommendation.
# Backend pipeline order. Each of the 7 frontend agents now maps 1:1 to a
# backend module — no more conjoined master_contract/extraction passes. The
# scope_agent (triage) + the 3 supporting clause extractors remain backend-only.
AGENT_LIST = ("hierarchy", "engagement_overview", "product_module", "scope_agent",
              "extraction", "material_match", "material_validation", "cpi",
              "term_renewal", "termination", "sla", "volume_tiers")

# Agents downstream of the scope agent — these honor the per-agent allowlist.
# Note: product_module is NOT scoped (it runs on every contract by design,
# same rationale as engagement_overview / hierarchy — it's discovery work).
SCOPED_AGENTS = ("extraction", "material_match", "cpi", "term_renewal",
                 "termination", "sla", "volume_tiers")

AGENT_DISPLAY = {
    "hierarchy":            "Hierarchy Agent",
    "engagement_overview":  "Engagement Overview Agent",
    "product_module":       "Product Module Agent",
    "scope_agent":          "Scope Triage (internal)",
    "extraction":           "Fee Description Agent",
    "material_match":       "Material Code Matching Agent",
    "material_validation":  "Material Validation Agent",
    "cpi":                  "CPI Terms Agent",
    "term_renewal":         "Term & Renewal (internal)",
    "termination":          "Termination Clause Agent",
    "sla":                  "SLA & Credits (internal)",
    "volume_tiers":         "Volume Tiers (internal)",
}


# ── Frontend agents (the 7 shown to users) ─────────────────────────────────────
# Each frontend agent now corresponds to ONE backend module — no conjoined
# Phase 1/2 passes. The backend-only agents (scope triage, term_renewal, sla,
# volume_tiers) still run during Load and still feed the chat context, they're
# just not surfaced as one of the 7.
FRONTEND_AGENTS = (
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


def frontend_agent_done(key: str, client: str) -> bool:
    """Has this user-facing agent produced output for `client`?

    Maps each of the 7 frontend agents onto its backend module's is_processed().
    Engagement Overview, Product Module, and Material Code Matching are now
    fully independent agents with their own output files."""
    from agents import (hierarchy, engagement_overview, product_module,
                        extraction, material_match, cpi, termination,
                        material_validation, mnr_template)
    checks = {
        "contract_hierarchy": hierarchy.is_processed,
        "contract_scope":     engagement_overview.is_processed,
        "product_module":     product_module.is_processed,
        "fee_digitization":   extraction.is_processed,
        "material_match":     material_match.is_processed,
        "material_validation": material_validation.is_processed,
        "cpi_terms":          cpi.is_processed,
        "termination_clause": termination.is_processed,
        "mnr_template":       mnr_template.is_processed,
    }
    fn = checks.get(key)
    try:
        return bool(fn(client)) if fn else False
    except Exception:
        return False


def frontend_done_count(client: str) -> int:
    """How many of the 7 frontend agents have run for `client`."""
    return sum(1 for k, _ in FRONTEND_AGENTS if frontend_agent_done(k, client))


def agent_status(agent: str, client: str) -> str:
    from agents import (hierarchy, engagement_overview, product_module,
                        scope_agent, extraction, material_match, cpi,
                        term_renewal, termination, sla, volume_tiers,
                        material_validation, mnr_template)
    modules = {
        "hierarchy":           hierarchy,
        "engagement_overview": engagement_overview,
        "product_module":      product_module,
        "scope_agent":         scope_agent,
        "extraction":          extraction,
        "material_match":      material_match,
        "material_validation": material_validation,
        "cpi":                 cpi,
        "term_renewal":        term_renewal,
        "termination":         termination,
        "sla":                 sla,
        "volume_tiers":        volume_tiers,
        "mnr_template":        mnr_template,
        # Legacy alias — keep working for any caller still passing the old key.
        "master_contract":     engagement_overview,
    }
    mod = modules.get(agent)
    if mod is None:
        return "unknown"
    return "done" if mod.is_processed(client) else "not_run"


_ICON  = {"done": "✅", "not_run": "⚪", "ready": "🟡", "no_input": "⚫", "error": "❌"}
_LABEL = {"done": "Done", "not_run": "Not run", "ready": "Input ready",
          "no_input": "No CPI file", "error": "Error"}


def _count_contracts(client: str) -> int:
    cache = OUTPUT_DIR / "hierarchy_cache.json"
    if not cache.exists():
        return 0
    try:
        with open(cache, encoding="utf-8") as f:
            data = json.load(f)
        return sum(1 for k in data if k.startswith(f"{client}/"))
    except Exception:
        return 0


def _refresh_context(clients: list[str]) -> None:
    with st.spinner("Loading contract data into chat context…"):
        st.session_state.ctx         = build_multi_client_context(clients)
        st.session_state.ctx_clients = list(clients)


def _find_cited_pdfs(message_text: str, pdf_options: list) -> list:
    """Identify which PDFs from `pdf_options` are referenced in an assistant
    message — used to render clickable "View in viewer" buttons under the
    message so the user can jump straight to the contract in the side panel.

    Strategy:
      • If the message contains a "Sources:" block, scan only that block
        (cleaner — body text might mention a contract name in passing).
      • Otherwise, scan the whole message.
      • For each PDF in pdf_options, match on the full filename. If not
        found, try the filename stem without extension (LLMs occasionally
        drop the .pdf suffix when listing sources).
      • Preserve citation order; dedupe by filename.

    Returns a list of (label, Path) tuples in the order they were cited.
    """
    if not message_text or not pdf_options:
        return []

    # Find the Sources section if present (case-insensitive, allows
    # "Sources:" or "Sources :" or "**Sources:**" / "## Sources").
    sources_match = re.search(
        r'(?im)^\s*(?:#+\s*|\*+\s*)?sources?\s*:?\s*(?:\*+\s*)?$',
        message_text,
    )
    if sources_match:
        search_in = message_text[sources_match.end():]
    else:
        search_in = message_text

    search_in_lower = search_in.lower()
    found = []
    seen = set()
    for label, pdf_path in pdf_options:
        name_full = pdf_path.name              # e.g. "FOO-Amendment-...pdf"
        name_stem = pdf_path.stem              # e.g. "FOO-Amendment-..."
        if pdf_path.name in seen:
            continue
        if name_full.lower() in search_in_lower or name_stem.lower() in search_in_lower:
            found.append((label, pdf_path))
            seen.add(pdf_path.name)
    return found


def _find_cited_invoices(message_text: str) -> list:
    """Identify SAP invoices referenced in an assistant message so the UI can
    render clickable invoice links (and a best-effort 'open in viewer' button).

    Strategy:
      • Pull the {invoice_document: url} registry captured on the most recent
        invoice-context build (snowflake_invoice.get_last_invoice_links()).
      • A registry invoice is "cited" if its document number appears anywhere
        in the message, OR its URL appears verbatim.
      • Also surface any bare http(s) URL the model printed under an [INVOICE]
        tag even if it's not in the registry (defensive — registry may have
        rolled over).

    Returns a list of (invoice_document, url) tuples, citation order, deduped.
    """
    if not message_text:
        return []
    try:
        import snowflake_invoice as _sf
        registry = _sf.get_last_invoice_links()       # {doc: url}
    except Exception:
        registry = {}

    found: list = []
    seen: set = set()
    low = message_text.lower()

    for doc, url in registry.items():
        if not doc or doc in seen:
            continue
        if doc.lower() in low or (url and url.lower() in low):
            found.append((doc, url))
            seen.add(doc)

    # Defensive: catch [INVOICE]-tagged bare URLs not in the registry.
    for m in re.finditer(r'https?://\S+', message_text):
        url = m.group(0).rstrip(').,;"\'')
        if url in {u for _, u in found}:
            continue
        # Only treat it as an invoice link if an [INVOICE] tag is nearby.
        ctx_start = max(0, m.start() - 60)
        if "[invoice]" in message_text[ctx_start:m.start()].lower():
            key = f"link:{url}"
            if key not in seen:
                found.append(("(invoice)", url))
                seen.add(key)
    return found


# ── Agent runners ─────────────────────────────────────────────────────────────

def _run_hier(client: str) -> None:
    from agents.hierarchy import run as hier_run
    _mark = run_metrics.snapshot(); _t0 = time.perf_counter(); _ok = False
    with st.status(f"⏳ Analyzing {client}…", expanded=True) as status:
        try:
            log_lines: list[str] = []
            result = hier_run(
                client,
                api_key=HIERARCHY_API_KEY,   # self-sufficient agent uses its own key
                hierarchy_model=st.session_state.hier_model,
                progress_lines=log_lines,
                core=st.session_state.selected_core,
            )
            if log_lines:
                st.code("\n".join(log_lines[-30:]), language="text")

            # Reject misleading "complete" when the agent didn't actually do
            # anything. The new agent raises on bad config, but defensively
            # also check that something was processed AND an Excel exists.
            n_total     = int(result.get("rows", 0) or 0)
            n_extracted = int(result.get("extracted", 0) or 0)
            n_cached    = int(result.get("cached", 0) or 0)
            n_failed    = int(result.get("failed", 0) or 0)
            excel_path  = Path(result.get("excel", ""))

            if n_total == 0:
                status.update(label=f"⚠ {client}: no contracts processed",
                              state="error")
            elif not excel_path.exists():
                status.update(label=f"⚠ {client}: no Excel written",
                              state="error")
            elif n_failed and n_failed == n_total:
                status.update(label=f"❌ {client}: all {n_failed} contracts failed",
                              state="error")
            else:
                summary = (f"{n_extracted} extracted, {n_cached} cached"
                           + (f", {n_failed} failed" if n_failed else ""))
                status.update(label=f"✅ {client} — Hierarchy complete ({summary})",
                              state="complete")
                _ok = True
        except Exception as e:
            status.update(label=f"❌ {client} failed: {e}", state="error")
            st.exception(e)
        finally:
            run_metrics.finalize(client, "hierarchy", "Hierarchy Agent", _t0, _mark,
                                 fallback_model=st.session_state.hier_model,
                                 status="complete" if _ok else "error")


def _scope_for(client: str, agent_name: str):
    """Return the scope agent's per-agent allowlist, or None to mean 'all'."""
    from agents.scope_agent import load_scope
    return load_scope(client, agent_name)


def _run_engagement_overview(client: str) -> None:
    """Phase 1 scope (signatories, addresses, contract summary, DocuSign id, etc.)."""
    from agents.engagement_overview import run as eo_run
    pb = st.empty()

    def _cb(msg: str) -> None:
        pb.info("⏳ " + msg)

    _mark = run_metrics.snapshot(); _t0 = time.perf_counter(); _ok = False
    with st.status(f"⏳ Engagement Overview: {client}…", expanded=True) as status:
        try:
            result = eo_run(
                client,
                api_key=MASTER_CONTRACT_API_KEY,
                model=MASTER_CONTRACT_MODEL,
                progress_callback=_cb,
                core=st.session_state.selected_core,
            )
            pb.empty()
            if result.get("status") != "complete":
                status.update(label=f"⚠ Engagement Overview: {result.get('status')}", state="error")
                return
            status.update(label=f"✅ Engagement Overview — {result.get('rows', 0)} contracts",
                          state="complete")
            _ok = True
        except Exception as e:
            pb.empty()
            status.update(label=f"❌ Engagement Overview failed: {e}", state="error")
        finally:
            run_metrics.finalize(client, "engagement_overview", "Engagement Overview Agent",
                                 _t0, _mark, fallback_model=MASTER_CONTRACT_MODEL,
                                 status="complete" if _ok else "error")


def _run_product_module(client: str) -> None:
    """Phase 2 product / schedule / module hierarchy. Independent of the
    Engagement Overview agent — runs its own Phase 1.5 manifest call."""
    from agents.product_module import run as pm_run
    pb = st.empty()

    def _cb(msg: str) -> None:
        pb.info("⏳ " + msg)

    _mark = run_metrics.snapshot(); _t0 = time.perf_counter(); _ok = False
    with st.status(f"⏳ Product Module: {client}…", expanded=True) as status:
        try:
            result = pm_run(
                client,
                api_key=MASTER_CONTRACT_API_KEY,
                model=MASTER_CONTRACT_MODEL,
                progress_callback=_cb,
                core=st.session_state.selected_core,
            )
            pb.empty()
            if result.get("status") != "complete":
                status.update(label=f"⚠ Product Module: {result.get('status')}", state="error")
                return
            status.update(label=f"✅ Product Module — {result.get('rows', 0)} product / module rows",
                          state="complete")
            _ok = True
        except Exception as e:
            pb.empty()
            status.update(label=f"❌ Product Module failed: {e}", state="error")
        finally:
            run_metrics.finalize(client, "product_module", "Product Module Agent",
                                 _t0, _mark, fallback_model=MASTER_CONTRACT_MODEL,
                                 status="complete" if _ok else "error")


def _run_scope_agent(client: str) -> None:
    from agents.scope_agent import run as scope_run
    pb = st.empty()

    def _cb(msg: str) -> None:
        pb.info("⏳ " + msg)

    _mark = run_metrics.snapshot(); _t0 = time.perf_counter(); _ok = False
    with st.status(f"⏳ Scope Triage: {client}…", expanded=True) as status:
        try:
            result = scope_run(
                client,
                api_key=SCOPE_AGENT_API_KEY,
                model=SCOPE_AGENT_MODEL,
                progress_callback=_cb,
                core=st.session_state.selected_core,
            )
            pb.empty()
            if result.get("status") != "complete":
                status.update(label=f"⚠ Scope Agent: {result.get('status')}", state="error")
                return
            n = sum(len(v) for v in (result.get("scopes") or {}).values())
            status.update(label=f"✅ Scope Triage — {n} contract assignments",
                          state="complete")
            _ok = True
        except Exception as e:
            pb.empty()
            status.update(label=f"❌ Scope Triage failed: {e}", state="error")
        finally:
            run_metrics.finalize(client, "scope_agent", "Scope Triage (internal)",
                                 _t0, _mark, fallback_model=SCOPE_AGENT_MODEL,
                                 status="complete" if _ok else "error")


def _resolve_dict_path() -> Optional[Path]:
    """Resolve the active material-code dictionary path: user override (Advanced
    settings → Dictionary .xlsx path) wins, else the Core's default."""
    if st.session_state.dict_path:
        return Path(st.session_state.dict_path)
    return default_dictionary_for(st.session_state.selected_core) or None


def _run_extr(client: str) -> None:
    """Fee Description Agent — Phase 1 dollar/textual-item extraction only.
    Matching (Material Code Matching Agent) is now a separate runner.

    Dispatch by Core:
      * PORTICO (and anything else) → ``agents.extraction``
      * DNA                        → ``agents.dna_extraction``

    Both modules write to the same output paths and expose the same
    public surface, so every downstream consumer (frontend_agent_done,
    sidecar readers, the Outputs tab) works uniformly across cores.

    Force-reloads the chosen module on every invocation so a fresh
    %%writefile patch picks up immediately — Streamlit's sys.modules
    cache otherwise keeps the previously-imported version across reruns."""
    import importlib
    core = (st.session_state.get("selected_core") or "").upper()
    if core == "DNA":
        import agents.dna_extraction as _ext_module
    else:
        import agents.extraction as _ext_module
    try:
        _ext_module = importlib.reload(_ext_module)
    except Exception:
        pass
    extr_run = _ext_module.run
    pb = st.empty()

    def _cb(msg: str) -> None:
        pb.info("⏳ " + msg)

    _mark = run_metrics.snapshot(); _t0 = time.perf_counter(); _ok = False
    with st.status(f"⏳ Fee Description: {client}…", expanded=True) as status:
        try:
            result = extr_run(
                client,
                api_key=EXTRACTION_API_KEY,
                extraction_model=st.session_state.extr_model,
                progress_callback=_cb,
                contracts=_scope_for(client, "extraction"),
                core=st.session_state.selected_core,
                # Year cutoff is set via the "PDF year cutoff" input in the
                # Agent Outputs tab (defaults to 2022). Contracts dated
                # before this are skipped at PDF-discovery time and the
                # agent still writes an empty Excel + sidecar so it's
                # treated as "ran" (no infinite re-trigger from Load).
                min_year=int(st.session_state.get("extraction_min_year", 2022)),
            )
            pb.empty()
            n = result.get("rows", 0)
            status.update(label=f"✅ Fee Description complete — {n} line items",
                          state="complete")
            _ok = True
        except Exception as e:
            pb.empty()
            status.update(label=f"❌ Fee Description failed: {e}", state="error")
        finally:
            run_metrics.finalize(client, "extraction", "Fee Description Agent",
                                 _t0, _mark,
                                 fallback_model=st.session_state.extr_model,
                                 status="complete" if _ok else "error")


def _run_material_match(client: str) -> None:
    """Material Code Matching Agent — reads extraction_output.xlsx, runs the
    dictionary matcher, writes a filtered material_match_output.xlsx.

    Dispatch by Core:
      * PORTICO → ``agents.material_match.run``  (thin re-export of
        ``agents.extraction.run_matching``)
      * DNA     → ``agents.dna_extraction.run_matching``

    DNA's matcher uses section normalization + an anchor-keyword index
    + contract-extracted material code validation, none of which apply
    to PORTICO. Both write to the same output path so the rest of the
    chatbot is core-agnostic.

    Force-reload as above so Streamlit's sys.modules cache can't hide a
    fresh patch from the running process."""
    import importlib
    core = (st.session_state.get("selected_core") or "").upper()
    if core == "DNA":
        import agents.dna_extraction as _mm_module
        try:
            _mm_module = importlib.reload(_mm_module)
        except Exception:
            pass
        match_run = _mm_module.run_matching
    else:
        import agents.material_match as _mm_module
        try:
            _mm_module = importlib.reload(_mm_module)
        except Exception:
            pass
        match_run = _mm_module.run
    pb = st.empty()

    def _cb(msg: str) -> None:
        pb.info("⏳ " + msg)

    _mark = run_metrics.snapshot(); _t0 = time.perf_counter(); _ok = False
    with st.status(f"⏳ Material Code Matching: {client}…", expanded=True) as status:
        try:
            dict_p = _resolve_dict_path()
            if dict_p:
                _cb(f"Using dictionary: {Path(dict_p).name}")
            else:
                _cb("No material-code dictionary set — skipping matching")

            result = match_run(
                client,
                api_key=EXTRACTION_API_KEY,
                matching_model=st.session_state.match_model,
                dictionary_path=dict_p,
                progress_callback=_cb,
                core=st.session_state.selected_core,
                # Forwarded for the sidecar metadata only (matching itself
                # doesn't filter by year). Lets the chatbot UI show "year
                # cutoff was N" alongside empty matching outputs.
                min_year=int(st.session_state.get("extraction_min_year", 2022)),
            )
            pb.empty()
            status_str = result.get("status", "")
            if status_str == "no_dictionary":
                status.update(label=f"⚠ Material Code Matching skipped — no dictionary set",
                              state="error")
                return
            if status_str == "no_extraction":
                status.update(label=f"⚠ Material Code Matching needs Fee Description first — run that agent",
                              state="error")
                return
            if status_str != "complete":
                status.update(label=f"⚠ Material Code Matching: {status_str}", state="error")
                return
            n  = result.get("rows", 0)
            tt = result.get("total", 0)
            status.update(label=f"✅ Material Code Matching complete — {n} of {tt} items matched",
                          state="complete")
            _ok = True
        except Exception as e:
            pb.empty()
            status.update(label=f"❌ Material Code Matching failed: {e}", state="error")
        finally:
            run_metrics.finalize(client, "material_match", "Material Code Matching Agent",
                                 _t0, _mark,
                                 fallback_model=st.session_state.match_model,
                                 status="complete" if _ok else "error")


def _run_material_validation(client: str) -> None:
    """Material Validation Agent — re-scores the Matching Agent's material codes
    against historical Snowflake invoice data and writes
    validated_material_output.xlsx with a GREEN/YELLOW/RED band + fallback code.

    When Snowflake is unreachable (off the Fiserv VDI / no snowflake_config.toml
    / connector missing) it SKIPS — writes no file — so the Material Code
    Matching output stays the system of record."""
    import agents.material_validation as _mv_module
    pb = st.empty()

    def _cb(msg: str) -> None:
        pb.info("⏳ " + msg)

    _mark = run_metrics.snapshot(); _t0 = time.perf_counter(); _ok = False
    with st.status(f"⏳ Material Validation: {client}…", expanded=True) as status:
        try:
            result = _mv_module.run(
                client,
                api_key=VALIDATION_API_KEY,
                progress_callback=_cb,
                core=st.session_state.selected_core,
            )
            pb.empty()
            status_str = result.get("status", "")
            if status_str == "no_snowflake":
                status.update(label="⚠ Material Validation skipped — SAP invoice "
                                    "data (Snowflake) unavailable", state="error")
                detail = result.get("detail", "")
                st.warning(
                    "**Material Validation needs the live SAP invoice data in "
                    "Snowflake.** " + (result.get("reason") or "Snowflake unavailable")
                    + (f" — {detail}" if detail else "")
                    + "\n\nThis agent runs only on the Fiserv VDI with a valid "
                    "`snowflake_config.toml`. The Material Code Matching output "
                    "remains the system of record until then.")
                return
            if status_str == "no_matching":
                status.update(label="⚠ Material Validation needs Material Code "
                                    "Matching first — run that agent", state="error")
                return
            if status_str == "no_history":
                status.update(label="⚠ Material Validation: no invoice history for "
                                    "this client", state="error")
                st.info(result.get("reason")
                        or "No SAP bill-to / invoice lines matched this client.")
                return
            if status_str == "no_model":
                status.update(label="⚠ Material Validation: semantic model "
                                    "unavailable", state="error")
                st.warning("The semantic model (all-mpnet-base-v2) could not be "
                           "loaded. On the VDI it must be pre-cached — run the "
                           "**Material Code Matching** agent first (it loads the "
                           "same model), or set `HF_HUB_OFFLINE=1`. "
                           + (result.get("detail") or ""))
                return
            if status_str != "complete":
                status.update(label=f"⚠ Material Validation: {status_str}", state="error")
                return
            b = result.get("bands", {}) or {}
            status.update(
                label=f"✅ Material Validation complete — {result.get('rows', 0)} rows "
                      f"(🟢 {b.get('GREEN', 0)} · 🟡 {b.get('YELLOW', 0)} · "
                      f"🔴 {b.get('RED', 0)})",
                state="complete")
            _ok = True
        except Exception as e:
            pb.empty()
            status.update(label=f"❌ Material Validation failed: {e}", state="error")
        finally:
            run_metrics.finalize(client, "material_validation",
                                 "Material Validation Agent", _t0, _mark,
                                 fallback_model=VALIDATION_MODEL,
                                 status="complete" if _ok else "error")


def _run_mnr(client: str) -> None:
    """MNR Template Agent — forensic fee extraction on the latest master
    agreement, material-code matching, and a SAP-ready MNR Excel draft
    (mnr_output.xlsx) with biller colour-coding.

    Needs a master agreement (Hierarchy / Engagement Overview help find it, but
    a filename heuristic also works) and a Core dictionary. The
    'Frequently Used Material Codes.xlsx' catalog is preferred but optional —
    without it the agent matches against the Core master dictionary alone."""
    import importlib
    import agents.mnr_template as _mnr_module
    try:
        _mnr_module = importlib.reload(_mnr_module)
    except Exception:
        pass
    try:
        from config import MNR_API_KEY as _MNR_KEY, MNR_MODEL as _MNR_MODEL
    except Exception:
        _MNR_KEY, _MNR_MODEL = EXTRACTION_API_KEY, "gpt-5.2-2025-12-11"

    pb = st.empty()

    def _cb(msg: str) -> None:
        pb.info("⏳ " + msg)

    _mark = run_metrics.snapshot(); _t0 = time.perf_counter(); _ok = False
    with st.status(f"⏳ MNR Template: {client}…", expanded=True) as status:
        try:
            result = _mnr_module.run(
                client,
                api_key=_MNR_KEY,
                model=_MNR_MODEL,
                progress_callback=_cb,
                core=st.session_state.selected_core,
                dictionary_path=_resolve_dict_path(),
            )
            pb.empty()
            status_str = result.get("status", "")
            if status_str == "no_pdfs":
                status.update(label="⚠ MNR: no input PDFs for this client",
                              state="error")
                return
            if status_str == "no_master_agreement":
                status.update(label="⚠ MNR: no master agreement found",
                              state="error")
                st.info("The MNR agent runs on the client's latest MASTER "
                        "AGREEMENT. None was found — run the **Hierarchy** "
                        "and/or **Engagement Overview** agents first so "
                        "documents are classified, then re-run MNR.")
                return
            if status_str == "no_master":
                status.update(label="⚠ MNR: no Core dictionary found",
                              state="error")
                st.info("MNR needs a Core dictionary (set one in the sidebar) "
                        "to match material codes.")
                return
            if status_str != "complete":
                status.update(label=f"⚠ MNR: {status_str}", state="error")
                return
            rows = result.get("rows", 0)
            if not rows:
                status.update(label="⚠ MNR ran but produced 0 rows (no fee "
                                    "items extracted from the master agreement)",
                              state="error")
                return
            status.update(
                label=f"✅ MNR complete — {rows} row(s) "
                      f"(extracted {result.get('stage1_items', 0)}, matched "
                      f"{result.get('stage2_matched', 0)})",
                state="complete")
            _ok = True
        except Exception as e:
            pb.empty()
            status.update(label=f"❌ MNR failed: {e}", state="error")
        finally:
            run_metrics.finalize(client, "mnr_template",
                                 "MNR Template Agent", _t0, _mark,
                                 fallback_model=_MNR_MODEL,
                                 status="complete" if _ok else "error")


def _run_clause(agent_name: str, client: str) -> None:
    """Generic runner for the four ClauseExtractor-based agents."""
    from agents import term_renewal, termination, sla, volume_tiers
    modules = {"term_renewal": term_renewal, "termination": termination,
               "sla": sla, "volume_tiers": volume_tiers}
    mod      = modules[agent_name]
    display  = AGENT_DISPLAY[agent_name]
    pb       = st.empty()

    def _cb(msg: str) -> None:
        pb.info("⏳ " + msg)

    _mark = run_metrics.snapshot(); _t0 = time.perf_counter(); _ok = False
    with st.status(f"⏳ {display}: {client}…", expanded=True) as status:
        try:
            result = mod.run(client, progress_callback=_cb,
                             contracts=_scope_for(client, agent_name),
                             core=st.session_state.selected_core)
            pb.empty()
            if result.get("status") != "complete":
                status.update(label=f"⚠ {display}: {result.get('status')}", state="error")
                return
            n = result.get("rows", 0)
            status.update(label=f"✅ {display} complete — {n} rows", state="complete")
            _ok = True
        except Exception as e:
            pb.empty()
            status.update(label=f"❌ {display} failed: {e}", state="error")
        finally:
            run_metrics.finalize(client, agent_name, display, _t0, _mark,
                                 status="complete" if _ok else "error")


def _run_cpi_agent(client: str) -> None:
    from agents.cpi import run_full as cpi_run_full
    pb = st.empty()

    def _cb(msg: str) -> None:
        pb.info("⏳ " + msg)

    _mark = run_metrics.snapshot(); _t0 = time.perf_counter(); _ok = False
    with st.status(f"⏳ CPI Terms: {client}…", expanded=True) as status:
        try:
            result = cpi_run_full(
                client,
                api_key=CPI_API_KEY,
                model=CPI_MODEL,
                progress_callback=_cb,
                contracts=_scope_for(client, "cpi"),
                core=st.session_state.selected_core,
            )
            pb.empty()
            if result.get("status") != "complete":
                status.update(label=f"⚠ CPI Terms: {result.get('status')}", state="error")
                return
            n = result.get("rows", 0)
            status.update(label=f"✅ CPI Terms complete — {n} records", state="complete")
            _ok = True
        except Exception as e:
            pb.empty()
            status.update(label=f"❌ CPI Terms failed: {e}", state="error")
        finally:
            run_metrics.finalize(client, "cpi", "CPI Terms Agent", _t0, _mark,
                                 fallback_model=CPI_MODEL,
                                 status="complete" if _ok else "error")


# ── SIDEBAR ───────────────────────────────────────────────────────────────────

with st.sidebar:
    ui.section_title("Workspace")

    # ── Core selection ────────────────────────────────────────────────────────
    cores = list_cores()
    if not cores:
        st.error("No Core folders found in `Input/`.\n"
                 "Expected layout: `Input/<Core>/<Client>/*.pdf` (e.g. `Input/PORTICO/...`).")
        st.stop()

    _prev_core = st.session_state.selected_core or cores[0]
    if _prev_core not in cores:
        _prev_core = cores[0]
    core = st.selectbox(
        "Core",
        options=cores,
        index=cores.index(_prev_core),
        help="Top-level grouping under Input/ (e.g. PORTICO, DNA).",
    )
    if core != st.session_state.selected_core:
        st.session_state.selected_core    = core
        st.session_state.selected_clients = []   # reset client picks when Core changes
        st.session_state.focus_clients    = []
        st.session_state.messages         = []
        st.session_state.ctx              = {}
        st.session_state.ctx_clients      = []

    # ── Client selection (filtered to the chosen Core) ────────────────────────
    clients_all = list_clients(core)
    if not clients_all:
        st.error(f"No client folders found in `Input/{core}/`.\n"
                 "Add a client subfolder with PDFs.")
        st.stop()

    select_all = st.checkbox("Select all clients", value=False, key="select_all_cb")

    if select_all:
        selected: list[str] = clients_all
    else:
        selected = st.multiselect(
            "Select clients",
            options=clients_all,
            default=[c for c in st.session_state.selected_clients if c in clients_all],
            label_visibility="collapsed",
            placeholder="Choose one or more clients…",
        )

    # Detect selection change → invalidate context, chat, and focus
    if sorted(selected) != sorted(st.session_state.selected_clients):
        st.session_state.selected_clients = list(selected)
        st.session_state.focus_clients    = list(selected)   # default focus = everything loaded
        st.session_state.messages         = []
        st.session_state.ctx              = {}
        st.session_state.ctx_clients      = []

    st.divider()

    # ── Per-client status badges ──────────────────────────────────────────────
    if selected:
        ui.section_title("Status")
        total = len(FRONTEND_AGENTS)
        for c in selected:
            done  = frontend_done_count(c)
            state = "done" if done == total else ("partial" if done > 0 else "not_run")
            st.markdown(ui.client_row(c, state, f"{done}/{total} agents"),
                        unsafe_allow_html=True)
        st.divider()

    # ── Load New Clients ──────────────────────────────────────────────────────
    # A client "needs loading" if any agent in AGENT_LIST hasn't run yet.
    def _needs_load(c: str) -> bool:
        return any(agent_status(a, c) != "done" for a in AGENT_LIST)

    unprocessed = [c for c in selected if _needs_load(c)]
    all_loaded  = bool(selected) and len(unprocessed) == 0

    load_btn = st.button(
        "⬇  Load New Clients",
        disabled=(not selected or all_loaded),
        use_container_width=True,
        type="primary",
        help="Runs the full pipeline — Hierarchy → Engagement Overview → "
             "Product Module → Fee Description → Material Code Matching → "
             "CPI Terms → Termination Clause — for any client not yet "
             "processed. Each agent is fully independent and skipped if its "
             "output already exists.",
    )

    if all_loaded and selected:
        st.caption("✅ All selected clients are already loaded")
    elif not selected:
        st.caption("Select at least one client above to load")
    else:
        plural = "s" if len(unprocessed) != 1 else ""
        st.caption(f"{len(unprocessed)} client{plural} will be processed "
                   "(Hierarchy → Engagement Overview → Product Module → "
                   "Fee Description → Material Code Matching → CPI Terms → "
                   "Termination Clause)")

    st.divider()

    # ── Advanced settings ─────────────────────────────────────────────────────
    with st.expander("⚙️  Advanced settings"):
        st.session_state.chat_model  = st.text_input("Chat model",       value=st.session_state.chat_model)
        st.session_state.hier_model  = st.text_input("Hierarchy model",  value=st.session_state.hier_model)
        st.session_state.extr_model  = st.text_input("Extraction model", value=st.session_state.extr_model)
        st.session_state.match_model = st.text_input("Matching model",   value=st.session_state.match_model)
        # Surface the resolved dictionary path so the user sees what's in effect.
        _core_default = default_dictionary_for(st.session_state.selected_core or "")
        _ph = (f"Using Core default: {_core_default.name}" if _core_default
               else "Optional — enables material-code matching")
        st.session_state.dict_path = st.text_input(
            "Dictionary .xlsx path",
            value=st.session_state.dict_path,
            placeholder=_ph,
            help=("Leave blank to use the Core's default dictionary (if configured). "
                  "Override here to point at a specific .xlsx — sheet name is "
                  "auto-detected as long as one sheet has both 'Description' and "
                  "'Material Code' columns."),
        )
        if not st.session_state.dict_path and _core_default:
            st.caption(f"📚 Using **{_core_default.name}** by default for the **"
                       f"{st.session_state.selected_core}** Core.")

        if selected:
            st.markdown("---")
            st.markdown("**Run individual agents**")

            # One row per (agent, runner, help-text). Rendered as a 2-column
            # grid below, in the same order the pipeline executes them.
            # Note: Hierarchy is included here so users can re-run the
            # contract-hierarchy discovery agent on demand (separate from the
            # PRODUCT-hierarchy work that lives inside the Master Contract
            # agent's Phase 2).
            def _make_clause_runner(_agent: str):
                # Bind agent name into a default arg so the loop's late-binding
                # doesn't make every closure point at the last value.
                return lambda c, _a=_agent: _run_clause(_a, c)

            # The user-facing agents — each fully independent with its own
            # backend module and output file.
            _FRONTEND_BTNS = [
                ("contract_hierarchy", "Hierarchy Agent", _run_hier,
                 "Per-contract metadata, dates, parties, and parent/child "
                 "links between documents."),
                ("contract_scope", "Engagement Overview Agent",
                 _run_engagement_overview,
                 "Scope: agreement type, client + provider name/address, "
                 "signatories, DocuSign envelope id, effective date, and a "
                 "plain-English contract summary (plus 7 SOW-specific fields "
                 "when the document is a Statement of Work)."),
                ("product_module", "Product Module Agent", _run_product_module,
                 "Product / schedule / module hierarchy within each contract "
                 "(Parent → Level → Product → Module). Routes among five "
                 "extraction paths — STRICT / SOW / ORDER / LICENSE / GENERIC — "
                 "based on the contract's document type."),
                ("fee_digitization", "Fee Description Agent", _run_extr,
                 "Every dollar-denominated fee line item with price, "
                 "checkbox state, page number, section header, and a "
                 "short explanation of how the checkbox was associated. "
                 "Matches the reference Contract Extraction notebook "
                 "byte-for-byte."),
                ("material_match", "Material Code Matching Agent",
                 _run_material_match,
                 "Matches each billable item from the Fee Description output "
                 "against the material-code dictionary. Output contains ONLY "
                 "the rows that received a code. Needs a Dictionary .xlsx set "
                 "above."),
                ("material_validation", "Material Validation Agent",
                 _run_material_validation,
                 "Re-scores each matched material code against historical SAP "
                 "invoice data in Snowflake; adds a confidence band "
                 "(green/yellow/red), a validation score, and a fallback code. "
                 "Runs after Material Code Matching; needs the Fiserv VDI / "
                 "Snowflake to be reachable."),
                ("cpi_terms", "CPI Terms Agent", _run_cpi_agent,
                 "Scans PDFs for CPI / annual-escalation language, then "
                 "formats the result."),
                ("termination_clause", "Termination Clause Agent",
                 _make_clause_runner("termination"),
                 "Termination clauses: for cause / for convenience, notice "
                 "periods, early-termination fees, survival."),
                ("mnr_template", "MNR Template Agent", _run_mnr,
                 "Forensic fee extraction on the latest master agreement + "
                 "material-code matching, producing a SAP-ready MNR Excel "
                 "draft (mnr_output.xlsx) with biller colour-coding. Needs a "
                 "master agreement and a Core dictionary; the 'Frequently Used "
                 "Material Codes.xlsx' catalog is optional."),
            ]

            def _render_agent_buttons(btns: list, client: str) -> None:
                for i in range(0, len(btns), 2):
                    cc1, cc2 = st.columns(2)
                    for col, btn in zip((cc1, cc2), btns[i:i + 2]):
                        agent_key, label, runner, helpstr = btn
                        with col:
                            if st.button(label,
                                         key=f"btn_{agent_key}_{client}",
                                         use_container_width=True,
                                         help=helpstr):
                                runner(client)
                                st.session_state.ctx = {}
                                st.session_state.ctx_clients = []
                                st.rerun()

            for c in selected:
                st.markdown(f"*{c}*")
                _render_agent_buttons(_FRONTEND_BTNS, c)


# ── Trigger load ──────────────────────────────────────────────────────────────
# Runs the full pipeline for each client, in order:
#   Hierarchy → Engagement Overview → Product Module → Scope Triage →
#   Fee Description → Material Code Matching → CPI Terms →
#   (backend) Term & Renewal, Termination, SLA, Volume Tiers.
# Each agent is skipped if its output already exists, so re-loading is cheap.
if load_btn and unprocessed:
    for c in unprocessed:
        # ─── Discovery stage: must run on ALL contracts to populate metadata ───
        if agent_status("hierarchy", c) != "done":
            _run_hier(c)
        # ─── Engagement Overview + Product Module run independently now ────────
        if agent_status("engagement_overview", c) != "done":
            _run_engagement_overview(c)
        if agent_status("product_module", c) != "done":
            _run_product_module(c)
        # ─── Scope triage (cheap text-only LLM, reads hierarchy + overview) ────
        if agent_status("scope_agent", c) != "done":
            _run_scope_agent(c)
        # ─── Scoped agents — each uses scope_agent.load_scope() under the hood ──
        if agent_status("extraction", c) != "done":
            _run_extr(c)
        # Material Code Matching is auto-skipped when no dictionary is set.
        if agent_status("material_match", c) != "done":
            _run_material_match(c)
        # Material Validation self-skips (writes no file) when Snowflake is
        # unreachable, so it's safe to call unconditionally during Load.
        if agent_status("material_validation", c) != "done":
            _run_material_validation(c)
        if agent_status("cpi", c) != "done":
            _run_cpi_agent(c)
        for a in ("term_renewal", "termination", "sla", "volume_tiers"):
            if agent_status(a, c) != "done":
                _run_clause(a, c)
        # MNR Template — SAP-ready draft from the latest master agreement.
        # Self-skips (writes no file) when no master agreement / dictionary is
        # available, so it's safe to call unconditionally during Load.
        if agent_status("mnr_template", c) != "done":
            _run_mnr(c)
    st.session_state.ctx         = {}
    st.session_state.ctx_clients = []
    st.rerun()


# ── MAIN CONTENT ──────────────────────────────────────────────────────────────
if not selected:
    ui.render_header()
    st.info("Select one or more clients in the sidebar to get started.")
    st.stop()

# Branded header with Core › Client breadcrumb
if len(selected) == 1:
    _client_crumb = selected[0]
elif len(selected) <= 3:
    _client_crumb = " · ".join(selected)
else:
    _client_crumb = f"{len(selected)} clients"
ui.render_header(ui.crumb(st.session_state.selected_core, _client_crumb))


def _portfolio_kpis(clients: list[str]) -> dict:
    """Aggregate headline metrics across the selected clients for the dashboard."""
    contracts = items = matched = value = 0
    agents_done = agents_total = 0
    for c in clients:
        contracts    += _count_contracts(c)
        agents_total += len(FRONTEND_AGENTS)
        agents_done  += frontend_done_count(c)
        if agent_status("extraction", c) == "done":
            try:
                df = pd.read_excel(str(OUTPUT_DIR / c / "extraction_output.xlsx"))
                items += len(df)
                if "Material Code" in df.columns:
                    mc = df["Material Code"].astype(str).str.strip()
                    matched += ((mc != "") & (mc.str.lower() != "nan")).sum()
                price_col = "Cleaned Price" if "Cleaned Price" in df.columns else "Price"
                if price_col in df.columns:
                    nums = pd.to_numeric(
                        df[price_col].astype(str).str.replace(r"[^0-9.\-]", "", regex=True),
                        errors="coerce")
                    value += float(nums.sum(skipna=True) or 0)
            except Exception:
                pass
    pct = int(round(100 * agents_done / agents_total)) if agents_total else 0
    return {"clients": len(clients), "contracts": contracts, "items": items,
            "matched": matched, "unmatched": max(0, items - matched),
            "value": value, "pipeline_pct": pct}


tab_dashboard, tab_outputs, tab_runs, tab_chat = st.tabs(
    ["Dashboard", "Agent Outputs", "Run Details", "Chat"])


# ─── Dashboard tab ────────────────────────────────────────────────────────────
with tab_dashboard:
    k = _portfolio_kpis(selected)
    _val = f"${k['value']:,.0f}" if k["value"] else "—"
    ui.kpi_row([
        (k["clients"],   "Clients", "accent"),
        (k["contracts"], "Contracts"),
        (k["items"],     "Line items"),
        (_val,           "Extracted value"),
        (k["matched"],   "Matched codes"),
        (k["unmatched"], "Unmatched", "warn" if k["unmatched"] else ""),
        (f"{k['pipeline_pct']}%", "Pipeline complete"),
    ])

    ui.section_title("Pipeline status by client")
    _n_agents = len(FRONTEND_AGENTS)
    for c in selected:
        done  = frontend_done_count(c)
        state = ("done" if done == _n_agents
                 else ("partial" if done > 0 else "not_run"))
        st.markdown(
            ui.client_row(c, state, f"{done}/{_n_agents} agents · "
                                    f"{_count_contracts(c)} contracts"),
            unsafe_allow_html=True,
        )


# ─── Agent Outputs tab ────────────────────────────────────────────────────────
with tab_outputs:
    from agents import (engagement_overview as _eo_mod,
                        product_module as _pm_mod,
                        material_match as _mm_mod,
                        material_validation as _mv_mod,
                        termination as _term_mod,
                        # `extraction` exposes read_extraction_meta /
                        # read_matching_meta — used below to surface the
                        # year-cutoff explanation when an empty output is
                        # rendered (e.g. all PDFs were pre-cutoff).
                        extraction)

    _XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def _dl_xlsx(path, fname, key):
        st.download_button("⬇ Excel", path.read_bytes(), file_name=fname,
                           mime=_XLSX_MIME, use_container_width=True, key=key)

    def _render_output(key: str, display: str, c: str) -> None:
        """Render one frontend agent's status + preview + download."""
        st.markdown(f"**{display}**")
        done = frontend_agent_done(key, c)

        if key == "contract_hierarchy":
            if not done:
                st.info("Click **Load New Clients** in the sidebar."); return
            n = _count_contracts(c)
            st.success(f"✅ {n} contract{'s' if n != 1 else ''}")
            excel_h = OUTPUT_DIR / c / "contracts_hierarchy.xlsx"
            if excel_h.exists():
                try:
                    st.dataframe(pd.read_excel(str(excel_h)),
                                 use_container_width=True, height=260)
                except Exception as ex:
                    st.caption(f"Preview error: {ex}")
                _dl_xlsx(excel_h, f"{c}_hierarchy.xlsx", f"dl_h_{c}")
            html_h = OUTPUT_DIR / c / "contracts_hierarchy.html"
            if html_h.exists():
                # Two columns: 'View' toggles the inline Plotly graph, 'Download'
                # saves the HTML file to disk. The 'View' button flips a
                # per-client session flag so the graph stays open across
                # reruns and the user doesn't have to keep re-clicking.
                _view_key  = f"__view_hier_graph_{c}"
                _showing   = st.session_state.get(_view_key, False)
                _btn_label = "🗙 Hide interactive graph" if _showing else "👁 View interactive graph"
                bcol1, bcol2 = st.columns(2)
                with bcol1:
                    if st.button(_btn_label, key=f"btn_view_hh_{c}",
                                 use_container_width=True,
                                 help="Show the Plotly hierarchy graph inline below."):
                        st.session_state[_view_key] = not _showing
                        st.rerun()
                with bcol2:
                    st.download_button("⬇ Download HTML", html_h.read_bytes(),
                                       file_name=f"{c}_hierarchy.html",
                                       mime="text/html",
                                       use_container_width=True,
                                       key=f"dl_hh_{c}")
                if st.session_state.get(_view_key, False):
                    try:
                        import streamlit.components.v1 as _components
                        _html_blob = html_h.read_text(encoding="utf-8")
                        _components.html(_html_blob, height=820, scrolling=True)
                    except Exception as ex:
                        st.error(f"Could not render the interactive graph inline: {ex}")
                        st.caption("Tip: use the **Download HTML** button on the "
                                   "right and open the file in any browser.")

        elif key == "contract_scope":
            if not done:
                st.info("Run **Engagement Overview Agent**, or click **Load New Clients**."); return
            p = _eo_mod.output_path(c)
            try:
                df = pd.read_excel(str(p))
                st.success(f"✅ {len(df)} contract{'s' if len(df) != 1 else ''}")
                st.dataframe(df, use_container_width=True, height=260)
                _dl_xlsx(p, f"{c}_engagement_overview.xlsx", f"dl_eo_{c}")
            except Exception as ex:
                st.error(f"Load error: {ex}")

        elif key == "product_module":
            if not done:
                st.info("Run **Product Module Agent**, or click **Load New Clients**."); return
            p = _pm_mod.output_path(c)
            try:
                df = pd.read_excel(str(p))
                if "Product" in df.columns:   # drop empty placeholder rows
                    prod = df["Product"].astype(str).str.strip()
                    df = df[(prod != "") & (prod.str.lower() != "nan")]
                st.success(f"✅ {len(df)} product / module row{'s' if len(df) != 1 else ''}")
                st.dataframe(df, use_container_width=True, height=260)
                _dl_xlsx(p, f"{c}_product_module.xlsx", f"dl_pm_{c}")
            except Exception as ex:
                st.error(f"Load error: {ex}")

        elif key == "fee_digitization":
            if not done:
                st.info("Run **Fee Description Agent**, or click **Load New Clients**."); return
            p = OUTPUT_DIR / c / "extraction_output.xlsx"
            try:
                df = pd.read_excel(str(p))
                if len(df) == 0:
                    # Empty extraction — keep the same vertical footprint as
                    # a populated card by stacking:
                    #   (1) a short status pill at the top (mirrors the
                    #       green `✅ N contracts` pill on populated cards),
                    #   (2) a bordered explanation box sized to match the
                    #       dataframe(260) + download_button(~40) stack.
                    # No empty dataframe, no useless download button.
                    meta = extraction.read_extraction_meta(c)
                    cutoff = meta.get("min_year", st.session_state.get(
                        "extraction_min_year", 2022))
                    status_str       = meta.get("status", "no_items")
                    failure_summary  = (meta.get("failure_summary") or "").strip()
                    files_in_folder  = meta.get("files_in_folder")
                    selected_files   = meta.get("selected_files")
                    meta_note        = (meta.get("note") or "").strip()

                    if status_str == "no_pdfs":
                        # Genuine year-filter exclusion — every PDF in the
                        # folder is pre-cutoff (or the folder is empty).
                        pill = (f"ℹ️ 0 items — all PDFs predate cutoff "
                                f"year **{cutoff}**")
                        body = (
                            f"**Why?**\n\n"
                            f"No contracts dated **≥ {cutoff}** were found "
                            f"in this client's input folder. All available "
                            f"PDFs are pre-{cutoff} and were skipped as "
                            f"legacy.\n\n"
                            f"**Fix**\n\n"
                            f"1. Change the `PDF year cutoff` input above "
                            f"to a smaller year.\n"
                            f"2. Re-run **Fee Description Agent** from the "
                            f"sidebar."
                        )
                    elif status_str == "no_items" and failure_summary:
                        # PDFs were processed (year filter passed) but the
                        # model API misbehaved on every chunk — show the
                        # actual reason instead of the generic pre-cutoff
                        # explanation.
                        pill = (f"⚠️ Extraction failed — model returned no "
                                f"usable response")
                        body = (
                            f"**Why?**\n\n"
                            f"The agent processed "
                            f"**{selected_files or '?'}** PDF(s) dated **≥ "
                            f"{cutoff}** (none were pre-cutoff). The model "
                            f"API failed on every page chunk:\n\n"
                            f"`{failure_summary}`\n\n"
                            f"**Fix**\n\n"
                            f"- If `empty_response`: the model returned no "
                            f"text. Often a transient issue — re-run the "
                            f"agent. If it persists, try a different "
                            f"extraction model.\n"
                            f"- If `api_error` / rate limit: wait and "
                            f"re-run.\n"
                            f"- Check the run log in the sidebar for the "
                            f"per-chunk warning details."
                        )
                    elif status_str == "no_items":
                        # Year filter passed AND the API worked — model
                        # actually saw the pages and reported no fees. Rare
                        # but possible (e.g. a non-fee-bearing amendment).
                        pill = (f"ℹ️ 0 items extracted "
                                f"({selected_files or '?'} PDFs scanned, "
                                f"cutoff {cutoff})")
                        body = (
                            f"**Why?**\n\n"
                            f"The agent processed **{selected_files or '?'}**"
                            f" PDF(s) dated **≥ {cutoff}** but the model "
                            f"reported **0 fee items** in any of them. The "
                            f"PDFs are NOT pre-cutoff — the model genuinely "
                            f"saw no fee schedule.\n\n"
                            f"**Fix**\n\n"
                            f"- Open the PDFs and verify they contain a "
                            f"fee schedule (not just legal text / body).\n"
                            f"- Ensure `marked_checkbox_example.png` is in "
                            f"the project root.\n"
                            f"- Try re-running the agent — the model is "
                            f"non-deterministic."
                        )
                    else:
                        pill = (f"ℹ️ Extraction completed with 0 items "
                                f"(cutoff {cutoff})")
                        body = (
                            f"Extraction ran successfully but produced "
                            f"**0 items**. Year cutoff in effect: "
                            f"**{cutoff}**.\n\n"
                            f"{meta_note}"
                        )
                    st.info(pill)
                    with st.container(border=True, height=260):
                        st.markdown(body)
                else:
                    st.success(f"✅ {len(df)} line item{'s' if len(df) != 1 else ''}")
                    st.dataframe(df, use_container_width=True, height=260)
                    _dl_xlsx(p, f"{c}_fee_digitalization.xlsx", f"dl_fee_{c}")
            except Exception as ex:
                st.error(f"Load error: {ex}")

        elif key == "material_match":
            p = _mm_mod.output_path(c)
            if not done:
                st.info("Set a Dictionary .xlsx in **Advanced settings**, then run "
                        "**Material Code Matching Agent** (or Load New Clients).")
                if not st.session_state.dict_path and not default_dictionary_for(
                        st.session_state.selected_core or ""):
                    st.caption("No dictionary set — material-code matching is skipped.")
                return
            try:
                df = pd.read_excel(str(p))
                # material_match_output.xlsx is already filtered to matched
                # rows only, so no further filtering is needed here.
                if len(df) == 0:
                    # Empty matching — mirror the empty-extraction layout:
                    # short status pill at top (in the green-success slot)
                    # + bordered explanation sized to match dataframe(260).
                    # No empty dataframe + no useless download button.
                    mm_meta = extraction.read_matching_meta(c)
                    ext_meta = extraction.read_extraction_meta(c)
                    cutoff = (mm_meta.get("min_year")
                              or ext_meta.get("min_year")
                              or st.session_state.get("extraction_min_year", 2022))
                    if mm_meta.get("status") == "no_extraction_items":
                        # Defer to extraction's real reason. If extraction
                        # failed because of an API error (not the year
                        # filter), say so — don't blame the cutoff.
                        ext_status   = ext_meta.get("status", "")
                        ext_failure  = (ext_meta.get("failure_summary") or "").strip()
                        if ext_failure:
                            pill = (f"⚠️ 0 matches — Fee Description "
                                    f"failed on the model side")
                            body = (
                                f"**Why?**\n\n"
                                f"Material Matching had **nothing to match** "
                                f"— Fee Description produced 0 items, but "
                                f"NOT because of the cutoff. The model API "
                                f"failed on every page chunk:\n\n"
                                f"`{ext_failure}`\n\n"
                                f"**Fix**\n\n"
                                f"1. Re-run **Fee Description Agent** "
                                f"(transient API issues often clear).\n"
                                f"2. If it persists, check the run log for "
                                f"per-chunk warnings.\n"
                                f"3. Once Fee Description produces rows, "
                                f"re-run **Material Code Matching Agent**."
                            )
                        elif ext_status == "no_pdfs":
                            pill = (f"ℹ️ 0 matches — Fee Description was "
                                    f"empty (cutoff {cutoff})")
                            body = (
                                f"**Why?**\n\n"
                                f"Material Matching had **nothing to "
                                f"match** — Fee Description produced 0 "
                                f"items because all contracts are dated "
                                f"before the cutoff (**{cutoff}**).\n\n"
                                f"**Fix**\n\n"
                                f"1. Change the `PDF year cutoff` input "
                                f"above.\n"
                                f"2. Re-run **Fee Description Agent**.\n"
                                f"3. Re-run **Material Code Matching Agent**."
                            )
                        else:
                            pill = (f"ℹ️ 0 matches — Fee Description "
                                    f"produced no items")
                            body = (
                                f"**Why?**\n\n"
                                f"Material Matching had **nothing to match** "
                                f"— Fee Description scanned the PDFs but "
                                f"the model reported no fee items. The "
                                f"PDFs are NOT pre-cutoff (cutoff "
                                f"**{cutoff}**).\n\n"
                                f"**Fix**\n\n"
                                f"- Open the PDFs and verify they contain "
                                f"a fee schedule.\n"
                                f"- Re-run **Fee Description Agent** — "
                                f"the model is non-deterministic.\n"
                                f"- Then re-run **Material Code Matching "
                                f"Agent**."
                            )
                    else:
                        pill = (f"ℹ️ 0 matched rows (cutoff {cutoff})")
                        body = (
                            f"Material Matching produced **0 matched rows**.\n\n"
                            f"Either no item crossed the confidence "
                            f"threshold, the dictionary didn't contain "
                            f"matching descriptions, or the input was "
                            f"empty. Year cutoff in effect: **{cutoff}**."
                        )
                    st.info(pill)
                    with st.container(border=True, height=260):
                        st.markdown(body)
                    # Skip the column-selector + dataframe + download path
                    # entirely for 0 rows.
                    return
                st.success(f"✅ {len(df)} item{'s' if len(df) != 1 else ''} "
                           "matched to a material code")
                wanted = [col for col in ("Item", "Price", "Material Code",
                                          "Matched Description", "Match Confidence",
                                          "Item Category", "Condition Type",
                                          "CPI Eligible")
                          if col in df.columns]
                st.dataframe(df[wanted] if wanted else df,
                             use_container_width=True, height=260)
                _dl_xlsx(p, f"{c}_material_match.xlsx", f"dl_mm_{c}")
            except Exception as ex:
                st.error(f"Load error: {ex}")

        elif key == "cpi_terms":
            if not done:
                st.info("Run **CPI Terms Agent**, or click **Load New Clients**."); return
            p = OUTPUT_DIR / c / "cpi_output.xlsx"
            try:
                df = pd.read_excel(str(p))
                st.success(f"✅ {len(df)} record{'s' if len(df) != 1 else ''}")
                st.dataframe(df, use_container_width=True, height=260)
                _dl_xlsx(p, f"{c}_cpi.xlsx", f"dl_cpi_{c}")
            except Exception as ex:
                st.error(f"Load error: {ex}")

        elif key == "termination_clause":
            if not done:
                st.info("Run **Termination Clause Agent**, or click **Load New Clients**."); return
            p = _term_mod.output_path(c)
            try:
                df = pd.read_excel(str(p))
                st.success(f"✅ {len(df)} contract{'s' if len(df) != 1 else ''} scanned")
                st.dataframe(df, use_container_width=True, height=260)
                _dl_xlsx(p, f"{c}_termination.xlsx", f"dl_term_{c}")
            except Exception as ex:
                st.error(f"Load error: {ex}")

        elif key == "material_validation":
            if not done:
                st.info("Run **Material Validation Agent** (needs Material Code "
                        "Matching first, plus a reachable Snowflake/VDI "
                        "connection), or click **Load New Clients**."); return
            p = _mv_mod.output_path(c)
            try:
                df = pd.read_excel(str(p))
                if "confidence_band" in df.columns:
                    bands = df["confidence_band"].astype(str).str.upper()
                    g = int((bands == "GREEN").sum())
                    y = int((bands == "YELLOW").sum())
                    r = int((bands == "RED").sum())
                    st.success(f"✅ {len(df)} validated — 🟢 {g} · 🟡 {y} · 🔴 {r}")
                else:
                    st.success(f"✅ {len(df)} validated")
                wanted = [col for col in ("Item", "Price", "old_material_code",
                                          "new_material_code", "new_validation_score",
                                          "confidence_band", "fallback_material_code",
                                          "invoice_cadence", "validation_reason")
                          if col in df.columns]
                st.dataframe(df[wanted] if wanted else df,
                             use_container_width=True, height=260)
                _dl_xlsx(p, f"{c}_validated_material.xlsx", f"dl_mv_{c}")
            except Exception as ex:
                st.error(f"Load error: {ex}")

    # ── PDF year cutoff selector (Fee Description + Material Matching) ──────
    # Contracts dated before this year are skipped by Fee Description as
    # "legacy". Default 2022 mirrors the colleague's reference notebook.
    # Stored at session scope so every Load / per-agent click after this
    # picks up the new value. Changing the year here does NOT auto-rerun
    # anything — agents whose outputs already exist still count as "done"
    # via is_processed. To re-extract with a new year, either delete the
    # client's extraction_output.xlsx or click the individual "Fee
    # Description Agent" button (which always re-runs).
    st.session_state.setdefault("extraction_min_year", 2022)
    _yr_col, _yr_help_col = st.columns([1, 4])
    with _yr_col:
        st.session_state.extraction_min_year = st.number_input(
            "PDF year cutoff",
            min_value=2000, max_value=2100, step=1,
            value=int(st.session_state.extraction_min_year),
            help=("Contracts dated before this year are skipped by the Fee "
                  "Description agent. Match Code Matching inherits the same "
                  "year cutoff. Default 2022."),
            key="_min_year_input",
        )
    with _yr_help_col:
        st.caption(
            f"📅 Currently extracting contracts dated **≥ "
            f"{int(st.session_state.extraction_min_year)}** only. Older "
            f"contracts are treated as legacy and skipped. If a client's "
            f"Fee Description / Material Matching shows '0 items' below, "
            f"lower this number and re-run those two agents (delete the "
            f"existing output Excel first, or use the individual agent "
            f"button which always re-runs)."
        )

    for c in selected:
        _total    = len(FRONTEND_AGENTS)
        _done_n   = frontend_done_count(c)
        _hdr_mark = "✓" if _done_n == _total else ("•" if _done_n > 0 else "·")
        header = f"{_hdr_mark}  {c} — {_done_n}/{_total} agents complete"
        with st.expander(header, expanded=(len(selected) == 1)):
            # ── Internal triage coverage banner (if scope triage has run) ──
            from agents import scope_agent as _sa
            if _sa.is_processed(c):
                try:
                    import json as _json
                    with open(_sa.output_path(c), encoding="utf-8") as _f:
                        _rep = _json.load(_f)
                    n_all = len(_rep.get("all_files", []))
                    summary = " · ".join(
                        f"{ag.replace('_', ' ').title()}: {len(_rep['scopes'].get(ag, []))}/{n_all}"
                        for ag in ("extraction", "cpi", "term_renewal",
                                   "termination", "sla", "volume_tiers")
                    )
                    st.caption(f"🎯 Per-agent contract coverage (internal triage) — {summary}")
                except Exception:
                    pass

            # The frontend agents, laid out 4 per row across two rows.
            for row in (FRONTEND_AGENTS[:4], FRONTEND_AGENTS[4:]):
                cols = st.columns(len(row))
                for col, (key, display) in zip(cols, row):
                    with col:
                        _render_output(key, display, c)
                st.markdown("---")


# ─── Run Details tab ──────────────────────────────────────────────────────────
# Per-agent run telemetry (runtime, model, input/output tokens) for the selected
# clients. Records are produced by run_metrics.finalize() inside each agent runner
# and persisted to Output/<client>/run_metrics.json, so this works whether the
# user ran all 9 agents or any subset — only the agents that actually ran appear.
with tab_runs:
    ui.section_title("Agent Run Details")
    st.caption(
        "Most recent run per agent for each selected client — runtime, "
        "**Model Requested** (what our code asked for), **Actual Model** "
        "(what the API echoed back in response.model), and input/output "
        "token usage. On the Fiserv VDI proxy the two model columns will "
        "often differ — the proxy is bound to whatever model is configured "
        "behind the X-Purpose tag; the requested name is just a routing hint."
    )

    def _fmt_list(xs):
        """Compact one-line render of a list of model names."""
        if not xs:
            return ""
        if isinstance(xs, str):
            return xs
        try:
            return ", ".join(sorted({str(x) for x in xs if x}))
        except Exception:
            return str(xs)

    any_runs = False
    for c in selected:
        latest = run_metrics.latest_by_agent(c)

        # ── Per-(agent, actual model) detail rows ─────────────────────────
        # Each agent gets ONE row per actual model the API actually served.
        # The "Model Requested" column shows what our code asked for; the
        # "Actual Model" column shows what response.model came back as.
        detail_rows: list[dict] = []
        for agent in AGENT_LIST:
            rec = latest.get(agent)
            if not rec:
                continue
            agent_label = rec.get("display") or AGENT_DISPLAY.get(agent, agent)
            timestamp   = rec.get("timestamp", "")
            runtime     = rec.get("runtime_s", 0)
            status      = rec.get("status", "")
            per_model: dict = rec.get("per_model") or {}

            if per_model:
                for bucket_key in sorted(per_model.keys()):
                    pm = per_model[bucket_key]
                    requested = _fmt_list(pm.get("requested_models")) or bucket_key
                    actual    = _fmt_list(pm.get("actual_models"))   or "(not echoed)"
                    detail_rows.append({
                        "Agent":           agent_label,
                        "Last Run":        timestamp,
                        "Runtime (s)":     runtime,
                        "Model Requested": requested,
                        "Actual Model":    actual,
                        "Calls":           int(pm.get("calls", 0)),
                        "Input tokens":    int(pm.get("input", 0)),
                        "Output tokens":   int(pm.get("output", 0)),
                        "Total tokens":    int(pm.get("total", 0)),
                        "Status":          status,
                    })
            else:
                # Legacy run record (no per_model) — show the aggregate. The
                # 'model' field on legacy records was the requested name.
                detail_rows.append({
                    "Agent":           agent_label,
                    "Last Run":        timestamp,
                    "Runtime (s)":     runtime,
                    "Model Requested": rec.get("model", ""),
                    "Actual Model":    "(legacy record — actual model not captured)",
                    "Calls":           int(rec.get("calls", 0)),
                    "Input tokens":    int(rec.get("input_tokens", 0)),
                    "Output tokens":   int(rec.get("output_tokens", 0)),
                    "Total tokens":    int(rec.get("total_tokens", 0)),
                    "Status":          status,
                })

        st.markdown(f"**{c}**")
        if not detail_rows:
            st.info("No agent runs recorded yet for this client. "
                    "Run one or more agents to populate run details.")
            continue

        any_runs = True

        # ── Detail table (one row per agent×actual-model) ─────────────────
        df_runs = pd.DataFrame(detail_rows)
        totals = {
            "Agent":           "TOTAL",
            "Last Run":        "",
            "Runtime (s)":     round(sum(r["Runtime (s)"] for r in detail_rows), 2),
            "Model Requested": "",
            "Actual Model":    "",
            "Calls":           sum(r["Calls"]         for r in detail_rows),
            "Input tokens":    sum(r["Input tokens"]  for r in detail_rows),
            "Output tokens":   sum(r["Output tokens"] for r in detail_rows),
            "Total tokens":    sum(r["Total tokens"]  for r in detail_rows),
            "Status":          "",
        }
        df_show = pd.concat([df_runs, pd.DataFrame([totals])], ignore_index=True)
        st.dataframe(df_show, use_container_width=True, hide_index=True)

        # ── Download as Excel ─────────────────────────────────────────────
        # The on-disk JSON (Output/<client>/run_metrics.json) stays as the
        # source of truth; this just gives the user a one-click .xlsx of
        # what they're currently looking at.
        try:
            from io import BytesIO as _BytesIO
            _buf = _BytesIO()
            with pd.ExcelWriter(_buf, engine="openpyxl") as _xw:
                df_show.to_excel(_xw, sheet_name="Run Details",
                                 index=False)
            _buf.seek(0)
            st.download_button(
                "⬇  Download run metrics (Excel)",
                _buf.getvalue(),
                file_name=f"{c}_run_metrics.xlsx",
                mime=("application/vnd.openxmlformats-officedocument."
                      "spreadsheetml.sheet"),
                key=f"dl_runmetrics_{c}",
                help="Saves the table above (including the TOTAL row) as a "
                     ".xlsx file. Use this for emailing run summaries or "
                     "feeding the numbers into a cost calculator.",
            )
        except Exception as _ex:
            st.caption(f"Could not build Excel download: {_ex}")

        # ── Per-model rollup (across all agents for this client) ──────────
        # Collapses everything to the ACTUAL model that served the work, so
        # cost calculations price the right model. Two extra columns show
        # the requested names that landed in each bucket and how many calls.
        by_actual: dict = {}
        for r in detail_rows:
            key = r["Actual Model"] or "(not echoed)"
            b   = by_actual.setdefault(key, {
                "calls": 0, "input": 0, "output": 0, "total": 0,
                "requested_names": set(),
            })
            b["calls"]  += r["Calls"]
            b["input"]  += r["Input tokens"]
            b["output"] += r["Output tokens"]
            b["total"]  += r["Total tokens"]
            for rn in (r["Model Requested"] or "").split(", "):
                rn = rn.strip()
                if rn:
                    b["requested_names"].add(rn)
        if by_actual:
            roll_rows = [
                {"Actual Model":    m,
                 "Requested As":    ", ".join(sorted(v["requested_names"])),
                 "Calls":           v["calls"],
                 "Input tokens":    v["input"],
                 "Output tokens":   v["output"],
                 "Total tokens":    v["total"]}
                for m, v in sorted(by_actual.items())
            ]
            with st.expander(f"💰 Per-model rollup for {c} "
                             f"(for cost calculation)", expanded=False):
                st.caption("Total tokens summed across every agent for this "
                           "client, grouped by **actual** model. The "
                           "'Requested As' column shows the names our code "
                           "asked for that the proxy collapsed onto this "
                           "model. Multiply input × prompt rate and output "
                           "× completion rate to compute the run cost.")
                st.dataframe(pd.DataFrame(roll_rows),
                             use_container_width=True, hide_index=True)

    if not any_runs and selected:
        st.caption("Tip: token counts come from the LLM gateway's usage block; "
                   "0 is shown if the backend doesn't return usage for a call.")

    # ── Rate-limit headers (Fiserv Foundation API only) ──────────────────
    # The Foundation API doesn't expose a "list my rate limits" endpoint, so
    # the only way to learn them is to read the HTTP response headers the
    # gateway returns. fiserv_client._capture_response_headers grabs the
    # interesting ones (x-ratelimit-*, x-ms-ratelimit-*, retry-after,
    # openai-*) after every successful call. Here we surface the latest
    # snapshot per X-Purpose tag so the user can see what the proxy is
    # currently enforcing.
    try:
        import fiserv_client as _fc
        _latest_headers = _fc.get_latest_response_headers()
    except Exception:
        _latest_headers = {}
    if _latest_headers:
        with st.expander("🚦 API rate-limit headers (last seen per purpose tag)",
                         expanded=False):
            st.caption(
                "These are response headers the Fiserv Foundation gateway "
                "returned on the most recent call routed to each X-Purpose "
                "tag. There's no separate 'describe my limits' API on the "
                "proxy — these headers are the only way to learn the active "
                "rate / token / request limits. **DPI and per-call image "
                "count limits are not enforced at the API level** — they're "
                "implicit through the per-request payload-size and "
                "context-window caps on whatever model is bound to the tag."
            )
            for purpose_key, snap in sorted(_latest_headers.items()):
                hdrs = snap.get("headers") or {}
                _rows = [{"Header": k, "Value": v} for k, v in sorted(hdrs.items())]
                st.markdown(
                    f"**Purpose: `{snap.get('purpose') or purpose_key}`** · "
                    f"endpoint `{snap.get('endpoint', '')}` · captured "
                    f"`{snap.get('captured_at', '')}`"
                )
                if _rows:
                    st.dataframe(pd.DataFrame(_rows),
                                 use_container_width=True, hide_index=True)
                else:
                    st.info(
                        "No rate-limit-shaped headers came back on the last "
                        "call to this purpose tag. The gateway may not be "
                        "forwarding them — ask the Foundation API team "
                        "whether `x-ratelimit-*` or `x-ms-ratelimit-*` "
                        "headers can be exposed downstream."
                    )


# ─── Chat tab ─────────────────────────────────────────────────────────────────
with tab_chat:

    # ── Focus picker (which loaded clients does the conversation cover?) ──
    if not st.session_state.focus_clients:
        st.session_state.focus_clients = list(selected)
    # Drop any stale focus entries that aren't in the current selection.
    current_focus = [c for c in st.session_state.focus_clients if c in selected]
    if not current_focus and selected:
        current_focus = list(selected)

    focus = st.multiselect(
        "Focus — which client(s) is this question / contract about?",
        options=selected,
        default=current_focus,
        key="focus_picker",
        help="Restrict the conversation context and the contract viewer to these "
             "clients.  Defaults to every loaded client, but you can narrow it down "
             "at any time without losing the chat.",
    )
    if not focus:
        st.info("Pick at least one client above to set the focus.")
        st.stop()

    # Detect focus change → invalidate context + chat history
    if sorted(focus) != sorted(st.session_state.focus_clients):
        st.session_state.focus_clients = list(focus)
        st.session_state.messages      = []
        st.session_state.ctx           = {}
        st.session_state.ctx_clients   = []

    # Rebuild context if focus drifted from what ctx was last built for
    if sorted(st.session_state.ctx_clients) != sorted(focus):
        _refresh_context(focus)

    ctx = st.session_state.ctx

    # Build the PDF options for every client in focus BEFORE the column split
    # so the chat column can also use it (for citation→viewer click-through).
    pdf_options: list = []   # list of (label, Path)
    for c in focus:
        cdir = _client_pdf_dir(c)
        for pdf in sorted(list(cdir.glob("*.pdf")) + list(cdir.glob("*.PDF")),
                          key=lambda p: p.name):
            pdf_options.append((f"{c} — {pdf.name}", pdf))

    # Apply any pending viewer-pick set by a "View" button click in the chat.
    # This MUST run before the selectbox below is instantiated so Streamlit
    # picks up the new value on this render.
    if "__pending_viewer_pick" in st.session_state:
        pending = st.session_state.pop("__pending_viewer_pick")
        labels_now = [lbl for lbl, _ in pdf_options]
        if pending in labels_now:
            st.session_state["viewer_pick"] = pending

    # Two-column layout: chat on the left, PDF viewer on the right.
    chat_col, pdf_col = st.columns([1, 1], gap="medium")

    # ── Right column: PDF viewer ──────────────────────────────────────────────
    with pdf_col:
        st.markdown(f"##### 📑 Contract viewer  <span style='font-size:0.78rem;color:#888'>· {len(focus)} client(s) in focus</span>",
                    unsafe_allow_html=True)

        # ── Invoice viewer (best-effort) ─────────────────────────────────────
        # When the user clicks "👁 Viewer" on a cited invoice, try to fetch the
        # invoice URL and render it inline. The invoice_url often points at an
        # internal document store that needs VDI network access / auth, so this
        # is best-effort: on any failure we fall back to the clickable link.
        _inv_view = st.session_state.get("__pending_invoice_view")
        if _inv_view:
            with st.container(border=True):
                top_l, top_r = st.columns([5, 1])
                with top_l:
                    st.markdown(f"🧾 **Invoice {_inv_view['doc']}**  "
                                f"[open in browser]({_inv_view['url']})")
                with top_r:
                    if st.button("✕", key="close_invoice_view",
                                 help="Close invoice viewer"):
                        st.session_state.pop("__pending_invoice_view", None)
                        st.rerun()
                rendered = False
                try:
                    import httpx
                    r = httpx.get(_inv_view["url"], timeout=20, follow_redirects=True)
                    r.raise_for_status()
                    ctype = r.headers.get("content-type", "").lower()
                    if "pdf" in ctype or _inv_view["url"].lower().endswith(".pdf"):
                        from streamlit_pdf_viewer import pdf_viewer
                        pdf_viewer(input=r.content, width="100%", height=640,
                                   key=f"inv_viewer_{_inv_view['doc']}")
                        st.download_button(
                            "⬇ Download invoice", r.content,
                            file_name=f"{_inv_view['doc']}.pdf",
                            mime="application/pdf", use_container_width=True,
                            key=f"inv_dl_{_inv_view['doc']}")
                        rendered = True
                    else:
                        st.info(f"Invoice URL returned `{ctype or 'unknown type'}`, "
                                "not a PDF — use the browser link above.")
                        rendered = True
                except Exception as _inv_e:
                    st.warning(
                        "Couldn't load the invoice inline (the URL may require "
                        "VDI network access or authentication). Use the browser "
                        f"link above.\n\n`{_inv_e}`")
                if rendered:
                    st.divider()

        if not pdf_options:
            st.info("No contract PDFs found for the selected clients.")
        else:
            labels = [lbl for lbl, _ in pdf_options]
            default_idx = (labels.index(st.session_state.get("viewer_pick"))
                           if st.session_state.get("viewer_pick") in labels else 0)
            pick = st.selectbox("Contract", options=labels, index=default_idx,
                                key="viewer_pick", label_visibility="collapsed")
            to_show = next(p for lbl, p in pdf_options if lbl == pick)

            try:
                from streamlit_pdf_viewer import pdf_viewer
                pdf_viewer(input=to_show.read_bytes(),
                           width="100%", height=720,
                           key=f"viewer_{to_show.name}")
            except Exception:
                st.caption("`streamlit-pdf-viewer` is not installed — using download fallback.")

            st.download_button("⬇ Download PDF", to_show.read_bytes(),
                               file_name=to_show.name, mime="application/pdf",
                               use_container_width=True, key=f"dl_{to_show.name}")

        # ── SAP invoice (Snowflake) connection diagnostic ────────────────────
        # On-demand only — connects when you click, never on every render. Use
        # this on the VDI to confirm the snowflake_config.toml + network path
        # before relying on invoice answers in chat.
        with st.expander("🧾 SAP invoice (Snowflake) connection", expanded=False):
            try:
                import snowflake_invoice as _sf
                if not _sf.snowflake_available():
                    st.warning("`snowflake-connector-python` is not installed in "
                               "this environment. Invoice answers are disabled. "
                               "Install it on the VDI to enable them.")
                else:
                    st.caption("Connector installed. Invoice context is fetched "
                               "only for questions mentioning invoice / billing / "
                               "SAP terms.")
                    if st.button("Test connection", key="sf_test_conn"):
                        # Streamlit holds modules in sys.modules across reruns,
                        # so a freshly-patched snowflake_invoice.py may still be
                        # the OLD version in memory. Force a reload on every
                        # click so the diagnostic always reflects the file on
                        # disk (otherwise we'd get a KeyError for new dict keys
                        # like can_query_table that the old module doesn't set).
                        import importlib
                        try:
                            _sf = importlib.reload(_sf)
                        except Exception:
                            pass

                        with st.spinner("Connecting to Snowflake…"):
                            status = _sf.connection_status()

                        # Every read is .get() so a partially-stale module
                        # never raises into the UI again.
                        config_found    = status.get("config_found", False)
                        config_source   = status.get("config_source", "?")
                        can_connect     = status.get("can_connect", False)
                        # NOTE: can_query_table is a NEW key (added with schema
                        # discovery). If absent, the loaded module predates the
                        # schema-discovery patch — flag it so the user re-applies.
                        has_schema_probe = "can_query_table" in status
                        can_query_table = status.get("can_query_table", False)
                        missing_required = status.get("missing_required", []) or []
                        missing_columns  = status.get("missing_columns", []) or []
                        aliased_columns  = status.get("aliased_columns", {}) or {}
                        err              = status.get("error", "")

                        # Layered diagnostics — show EXACTLY where the chain broke.
                        if not config_found:
                            st.error(f"No usable config:\n\n`{err}`")
                        elif not can_connect:
                            st.error(
                                f"Config found ({config_source}) but "
                                f"connection failed:\n\n`{err}`\n\n"
                                "Common causes: not on the Fiserv VDI "
                                "PrivateLink network, expired PAT, or wrong "
                                "account identifier.")
                        elif not has_schema_probe:
                            # Old snowflake_invoice.py is still loaded — the
                            # schema-discovery patch never took effect.
                            st.success(
                                f"✅ Connected. Config: {config_source}")
                            st.warning(
                                "Your `snowflake_invoice.py` is the older "
                                "version (no schema-discovery / column-alias "
                                "logic). To enable the SQL-error fix:\n\n"
                                "1. Re-run **Apply Snowflake Invoice Patch.ipynb** "
                                "so the new `snowflake_invoice.py` is written.\n"
                                "2. **Fully stop Streamlit** (Ctrl-C in the "
                                "terminal) and start it again — a hot-reload "
                                "is not enough; Python caches the old module.")
                        elif not can_query_table:
                            st.error(
                                f"Connected, but could not probe the configured "
                                f"view:\n\n`{err}`\n\n"
                                "Check that the database / schema / table in "
                                "snowflake_config.toml exist and your role has "
                                "SELECT on them.")
                        elif missing_required:
                            st.error(
                                "Required columns are missing from the view: "
                                + ", ".join(missing_required)
                                + ". Invoice queries cannot run.")
                        else:
                            st.success(
                                f"✅ Connected and view is queryable. "
                                f"Config: {config_source}")
                            if aliased_columns:
                                st.info(
                                    "Alias-mapped columns (canonical ← actual "
                                    "in view): "
                                    + ", ".join(
                                        f"`{k}` ← `{v}`"
                                        for k, v in aliased_columns.items()))
                            if missing_columns:
                                st.warning(
                                    "These optional columns are not in the "
                                    "view and will be returned as empty in "
                                    "invoice answers: "
                                    + ", ".join(f"`{c}`" for c in missing_columns))
            except Exception as _e:
                st.error(f"Invoice module unavailable: {_e}")

    # ── Left column: chat ─────────────────────────────────────────────────────
    with chat_col:
        # ── Compact header (single row so it doesn't eat into chat height) ───
        if len(focus) == 1:
            _focus_label = f"**{focus[0]}**"
        else:
            _focus_label = f"**{len(focus)} clients** in focus"
        loaded = [c for c in focus if agent_status("hierarchy", c) == "done"]

        hdr_l, hdr_r = st.columns([5, 1], gap="small")
        with hdr_l:
            st.markdown(
                f"##### 💬 Conversation  <span style='font-size:0.78rem;color:#888'>"
                f"· {_focus_label}</span>",
                unsafe_allow_html=True,
            )
            if loaded:
                st.caption(f"📊 Loaded: {', '.join(loaded)}")
            else:
                st.caption("⚠ No clients in focus have been loaded yet — "
                           "click **Load New Clients** in the sidebar.")
        with hdr_r:
            if st.button("🔄 Refresh", use_container_width=True,
                         help="Reload contract data and reset the conversation"):
                _refresh_context(focus)
                st.session_state.messages = []
                st.rerun()

        # ── Welcome message (built once per fresh chat) ───────────────────────
        if not st.session_state.messages:
            # Driven by the 7 user-facing agents. A capability is "available"
            # when at least one focused client has that agent's output; for a
            # single focused client this is exact. The "What I can't answer yet"
            # block is shown only while some agent is still missing — once all 7
            # have run for the focus, it's omitted entirely.
            AGENT_CAPABILITIES = [
                ("contract_hierarchy", "Hierarchy",
                 "document types, effective dates, parties, and how amendments "
                 "relate to the master agreement"),
                ("contract_scope", "Engagement Overview",
                 "per-contract addresses, signatories, document type, and a "
                 "plain-English contract summary"),
                ("product_module", "Product Module",
                 "the products, schedules, and modules within each contract "
                 "(Parent → Level → Product → Module)"),
                ("fee_digitization", "Fee Description",
                 "every fee line item — dollar values and Included / Waived / "
                 "By-Quote statuses — with price, checkbox state, and section"),
                ("material_match", "Material Code Matching",
                 "the SAP material code matched to each billable line item"),
                ("material_validation", "Material Validation",
                 "material codes re-scored against historical SAP invoice data, "
                 "with a confidence band (green/yellow/red) and a fallback code"),
                ("cpi_terms", "CPI Terms",
                 "annual escalation terms — eligibility dates, floors / caps, "
                 "and notice requirements"),
                ("termination_clause", "Termination Clause",
                 "for-cause vs. for-convenience termination, notice periods, "
                 "early-termination fees, and survival clauses"),
            ]

            can_answer:  list[str] = []
            cant_answer: list[str] = []
            for key, label, blurb in AGENT_CAPABILITIES:
                done_clients = [c for c in focus if frontend_agent_done(key, c)]
                if done_clients:
                    suffix = (f" — loaded for {len(done_clients)} client(s)"
                              if len(focus) > 1 else "")
                    can_answer.append(f"**{label}:** {blurb}{suffix}")
                else:
                    cant_answer.append(f"**{label}:** {blurb}")

            # Always-available knowledge (not one of the 7 agents, never gated).
            can_answer.append(
                "**Fiserv SAP billing reference:** item categories, condition "
                "types, material code families, revenue recognition"
            )

            can_block = "\n".join(f"- {x}" for x in can_answer)

            # Compact focus header — no long inline list.
            if len(focus) == 1:
                focus_line = f"**Focus:** `{focus[0]}`"
            else:
                focus_line = (f"**Focus:** {len(focus)} clients — "
                              + ", ".join(f"`{c}`" for c in focus[:3])
                              + (f", +{len(focus) - 3} more" if len(focus) > 3 else ""))

            welcome = (
                f"Hello! I'm your Contract Intelligence Assistant.\n\n"
                f"{focus_line}\n\n"
                f"**What I can answer right now:**\n{can_block}\n\n"
            )
            # Show "What I can't answer yet" only until all 7 agents have run.
            if cant_answer:
                cant_block = "\n".join(f"- {x}" for x in cant_answer)
                welcome += f"**What I can't answer yet:**\n{cant_block}\n\n"
            else:
                welcome += ("✅ All 7 agents have run for this focus — full "
                            "contract intelligence is available.\n\n")
            welcome += (
                "Ask anything about the clients above — pricing, amendments, CPI, "
                "SAP material codes, or contract terms. To switch which client(s) "
                "the conversation is about, use the **Focus** picker above."
            )
            st.session_state.messages.append({"role": "assistant", "content": welcome})

        # ── Scrollable chat window (ChatGPT/Claude style) ─────────────────────
        # All conversation lives inside this fixed-height container; only the
        # container scrolls, never the whole page. The chat_input below stays
        # pinned at the bottom of the column. Height roughly matches the PDF
        # viewer (720) minus the compact header + input row.
        chat_box = st.container(height=580, border=False)

        with chat_box:
            for msg_idx, msg in enumerate(st.session_state.messages):
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

                    # For assistant replies, render a "View in viewer" button
                    # for every contract cited in the message. Clicking the
                    # button loads that contract in the PDF viewer on the right.
                    if msg["role"] == "assistant":
                        if pdf_options:
                            cited = _find_cited_pdfs(msg["content"], pdf_options)
                            if cited:
                                st.caption("📂 Open cited contracts in the viewer:")
                                for i in range(0, len(cited), 2):
                                    btn_cols = st.columns(2)
                                    for col, (label, pdf_path) in zip(btn_cols, cited[i:i + 2]):
                                        with col:
                                            # Trim long filenames so the button fits.
                                            short = pdf_path.name
                                            if len(short) > 55:
                                                short = short[:52] + "…"
                                            if st.button(
                                                f"📄 {short}",
                                                key=f"view_{msg_idx}_{pdf_path.name}",
                                                use_container_width=True,
                                                help=f"Load {pdf_path.name} in the PDF viewer →",
                                            ):
                                                st.session_state["__pending_viewer_pick"] = label
                                                st.rerun()

                        # Invoice citations → clickable links + a best-effort
                        # "open in viewer" button (the right column tries to
                        # fetch & render the PDF; falls back to the link if the
                        # URL isn't directly reachable from this environment).
                        inv_cited = _find_cited_invoices(msg["content"])
                        if inv_cited:
                            st.caption("🧾 Cited SAP invoices:")
                            for inv_doc, inv_url in inv_cited:
                                link_col, btn_col = st.columns([3, 1])
                                with link_col:
                                    if inv_url:
                                        st.markdown(
                                            f"🧾 **{inv_doc}** — [open invoice]({inv_url})")
                                    else:
                                        st.markdown(f"🧾 **{inv_doc}** (no URL on record)")
                                with btn_col:
                                    if inv_url and st.button(
                                        "👁 Viewer",
                                        key=f"inv_{msg_idx}_{inv_doc}_{hash(inv_url) & 0xffff}",
                                        use_container_width=True,
                                        help="Try to render this invoice PDF in the viewer →",
                                    ):
                                        st.session_state["__pending_invoice_view"] = {
                                            "doc": inv_doc, "url": inv_url}
                                        st.rerun()

            # If the user submitted a question on the previous rerun, render
            # the assistant bubble + spinner INSIDE the scroll container (so
            # the "Thinking…" indicator appears above the chat input, not
            # below it) and generate the reply right here. This is the
            # ChatGPT/Claude pattern: input stays anchored at the bottom and
            # all activity happens in the scrolling history above it.
            if st.session_state.get("__needs_reply"):
                with st.chat_message("assistant"):
                    # ── Invoice gating: only consult Snowflake/SAP when the
                    # latest user question is about invoices / billing / SAP.
                    # On any other question we skip the (slow) round-trip and
                    # the prompt's invoice slot stays empty. Never fatal — a
                    # lookup error becomes a short notice the model relays.
                    last_user = next(
                        (m["content"] for m in reversed(st.session_state.messages)
                         if m["role"] == "user"), "")
                    invoice_ctx = ""
                    try:
                        import snowflake_invoice as _sf
                        if _sf.is_invoice_query(last_user):
                            with st.spinner("Referencing SAP invoice data (Snowflake)…"):
                                invoice_ctx = _sf.build_invoice_context(focus, last_user)
                    except Exception as _sf_err:
                        invoice_ctx = (
                            "=== SAP INVOICE DATA ===\n"
                            f"[INVOICE LOOKUP ERROR] {_sf_err}\n"
                            "Do NOT paraphrase this as a generic 'SQL error' or "
                            "'data pull issue' — quote the error verbatim so "
                            "the user can act on it, then answer the rest from "
                            "contract data if you can.")

                    with st.spinner("Thinking…"):
                        engine = ChatEngine(
                            api_key=CHAT_API_KEY,
                            model=st.session_state.chat_model,
                        )
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
                        # Build history — skip the auto-welcome message. The
                        # user's just-submitted question is already the last
                        # entry in st.session_state.messages.
                        history = [
                            m for m in st.session_state.messages
                            if not (m["role"] == "assistant"
                                    and "I'm your Contract Intelligence Assistant" in m["content"])
                        ]
                        try:
                            reply = engine.chat(history, system_prompt)
                        except Exception as e:
                            reply = (
                                f"⚠️ Chat error: {e}\n\n"
                                "Check the model name in Advanced settings."
                            )
                    st.markdown(reply)
                st.session_state.messages.append({"role": "assistant", "content": reply})
                st.session_state.pop("__needs_reply", None)
                # Rerun so the new reply renders through the loop above and
                # picks up its citation "View in viewer" buttons.
                st.rerun()

        # ── Chat input — bottom of the chat column, below the scroll area ────
        # Each new question is appended to history and a one-shot
        # __needs_reply flag triggers the assistant bubble in the container
        # above on the next rerun.
        _placeholder = (
            f"Ask about {focus[0]}'s contracts…" if len(focus) == 1
            else f"Ask about contracts for {len(focus)} clients in focus…"
        )
        if user_input := st.chat_input(_placeholder, key="chat_input_main"):
            st.session_state.messages.append({"role": "user", "content": user_input})
            st.session_state["__needs_reply"] = True
            st.rerun()
