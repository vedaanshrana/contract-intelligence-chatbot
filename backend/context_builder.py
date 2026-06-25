"""
Builds natural-language context strings from pipeline output files.
These strings are embedded into the chat system prompt so the model has
full visibility into the client's extracted contract data.
"""

import json
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from config import (
    OUTPUT_DIR,
    KB_PATH,
    KB_KEY_SECTION_NUMS,
    KB_MAX_SECTION_CHARS,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_page(raw) -> str:
    """Normalise a Page / Page Number cell into a clean string for citations.

    Excel often stores page numbers as floats (1.0, 2.0, ...) because of mixed
    types in the column. Strip the trailing '.0' and skip blanks / 'nan' so the
    LLM doesn't cite "p.nan" or "p.5.0".
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", "0"):
        return ""
    # Normalise "5.0" → "5"
    if s.endswith(".0"):
        s = s[:-2]
    return s


# ── Knowledge-base ─────────────────────────────────────────────────────────────

def load_kb_sections() -> str:
    """
    Extract the most chat-relevant sections from the FD306 knowledge base.
    Returns sections 8, 9, 11, 14, 15, 17 (SAP items, condition types,
    material codes, pricing recon, revenue leakage, CPI).
    Each section is capped at KB_MAX_SECTION_CHARS to control prompt length.
    """
    if not KB_PATH.exists():
        return ""

    text     = KB_PATH.read_text(encoding="utf-8")
    sections = re.split(r'\n(?=## \d+\.)', text)

    out_parts = []
    for section in sections:
        m = re.match(r'^## (\d+)\.', section)
        if m and m.group(1) in KB_KEY_SECTION_NUMS:
            out_parts.append(section[:KB_MAX_SECTION_CHARS])

    return "\n\n---\n\n".join(out_parts)


# ── Hierarchy ──────────────────────────────────────────────────────────────────

def build_hierarchy_context(client_name: str) -> str:
    """
    Load hierarchy output for a client and return a readable summary.
    Pulls from:
      1. hierarchy_cache.json  – raw per-contract extraction metadata
      2. contracts_hierarchy.xlsx – resolved parent-child tree

    Always prepends the contract-status legend so the chat LLM knows what
    the [ACTIVE] / [SUPERSEDED] / [ROOT-PARTIAL] / [ORPHAN] tags on every
    contract reference mean — even when the metadata block itself ends up
    empty for this client.
    """
    cache_path = OUTPUT_DIR / "hierarchy_cache.json"
    excel_path = OUTPUT_DIR / client_name / "contracts_hierarchy.xlsx"
    status_map = _get_contract_status(client_name)

    # The legend always leads. Other context blocks reference it without
    # repeating it, so it must be reachable even if no hierarchy data exists
    # yet (in which case every tag elsewhere will be [STATUS UNKNOWN]).
    lines: list[str] = [_STATUS_LEGEND.rstrip()]

    # ── From cache: raw metadata ──────────────────────────────────────────────
    if cache_path.exists():
        try:
            with open(cache_path, encoding="utf-8") as f:
                full_cache: dict = json.load(f)
        except Exception:
            full_cache = {}

        client_entries = {k: v for k, v in full_cache.items()
                         if k.startswith(f"{client_name}/")}

        if client_entries:
            lines.append(
                f"\n=== EXTRACTED CONTRACT METADATA ({len(client_entries)} documents) ==="
            )
            # Sort entries ACTIVE-first so the LLM sees current sources before
            # superseded / orphaned ones.
            fnames_sorted = _sort_filenames_by_status(
                (k.split("/", 1)[1] for k in client_entries),
                status_map,
            )
            for fname in fnames_sorted:
                key  = f"{client_name}/{fname}"
                meta = client_entries.get(key) or {}
                ctype  = (meta.get("contract_type") or "Unknown")
                sdate  = (meta.get("signed_date") or "?")
                edate  = (meta.get("effective_date") or "?")
                conf   = (meta.get("extraction_confidence") or "low")
                status = _status_tag(fname, status_map)

                parties = meta.get("parties") or []
                party_str = " / ".join(str(p) for p in parties[:3]) if parties else "?"

                # Gather product names from nested section_structure
                struct   = meta.get("section_structure") or []
                products = _walk_products(struct)[:12]

                lines.append(
                    f"\n{status} [{ctype}] {fname}\n"
                    f"  Signed: {sdate}  |  Effective: {edate}\n"
                    f"  Parties: {party_str}  |  Confidence: {conf}"
                )
                if products:
                    lines.append(f"  Products/Services: {', '.join(products)}")

                refs = meta.get("parent_references") or []
                if refs:
                    lines.append(f"  Parent ref: {str(refs[0])[:120]}")

    # ── From Excel: resolved hierarchy tree ───────────────────────────────────
    if excel_path.exists():
        try:
            df = pd.read_excel(str(excel_path))
            lines.append(f"\n=== HIERARCHY TREE ({len(df)} contracts) ===")
            lines.append(
                "Indented by Hierarchy_Level (0=root); each line shows the "
                "status tag, contract type, filename, signed date, parent, "
                "and resolution method. Use the status tag to decide what is "
                "currently in force."
            )
            for _, row in df.iterrows():
                level  = row.get("Hierarchy_Level", row.get("hierarchy_level", 0))
                try: level = int(level)
                except Exception: level = 0
                indent  = "  " * max(0, level)
                ctype   = str(row.get("Contract_Type", row.get("contract_type", "?")))
                # IMPORTANT: status_map is keyed by the FULL filename. Look up
                # the tag BEFORE truncating, otherwise everything renders as
                # [STATUS UNKNOWN].
                fname_full = str(row.get("Filename", row.get("filename", "?")))
                fname   = fname_full[:70]
                sdate   = str(row.get("Signed_Date",   row.get("signed_date", "?")))
                method  = str(row.get("Hierarchy_Method", ""))
                # New analyzer writes Parent_Contract; older builds wrote
                # Parent_Filename. Accept either so this line keeps working
                # whichever Excel landed on disk.
                parent  = str(row.get("Parent_Contract",
                              row.get("Parent_Filename", "")))
                par_str = f" ← {parent[:50]}" if parent and parent not in ("nan", "-", "") else ""
                met_str = f" [{method}]" if method and method != "nan" else ""
                status  = _status_tag(fname_full, status_map)
                lines.append(f"{indent}{status} [{ctype}] {fname} ({sdate}){par_str}{met_str}")

            # ── Active-contracts cheat sheet ─────────────────────────────────
            # Surface ACTIVE leaves as a flat list so the LLM can pick the
            # right current source without scanning the tree. ROOT-PARTIAL
            # roots are listed underneath so engagement-wide terms (defs,
            # indemnification, governing law) remain easy to find.
            actives = [f for f, info in status_map.items()
                       if info.get("status") == "ACTIVE"]
            roots   = [f for f, info in status_map.items()
                       if info.get("status") == "ROOT-PARTIAL"]
            orphans = [f for f, info in status_map.items()
                       if info.get("status") == "ORPHAN"]
            if actives or roots or orphans:
                lines.append("\n=== CURRENTLY-IN-FORCE CONTRACTS (use these first) ===")
                if actives:
                    lines.append(
                        f"[ACTIVE] leaves — {len(actives)} contract(s); "
                        "their terms / fees / clauses are CURRENT:"
                    )
                    for f in _sort_filenames_by_status(actives, status_map):
                        info = status_map.get(f, {})
                        sd   = info.get("signed_date") or "?"
                        par  = info.get("parent") or ""
                        par_str = f"  ← {par[:50]}" if par else "  (root leaf — no parent in corpus)"
                        lines.append(f"  • {f} (signed {sd}){par_str}")
                if roots:
                    lines.append(
                        f"\n[ROOT-PARTIAL] roots — {len(roots)} contract(s); "
                        "base terms apply WHERE NOT amended by descendants:"
                    )
                    for f in _sort_filenames_by_status(roots, status_map):
                        info = status_map.get(f, {})
                        sd   = info.get("signed_date") or "?"
                        lines.append(f"  • {f} (signed {sd})")
                if orphans:
                    lines.append(
                        f"\n[ORPHAN] — {len(orphans)} contract(s); could NOT "
                        "be linked to a parent. Treat with caution:"
                    )
                    for f in _sort_filenames_by_status(orphans, status_map):
                        info = status_map.get(f, {})
                        sd   = info.get("signed_date") or "?"
                        lines.append(f"  • {f} (signed {sd})")
        except Exception as e:
            lines.append(f"[Could not read hierarchy Excel: {e}]")

    # If both sources are missing, tell the user — but still keep the legend so
    # any other context block's tags are interpretable.
    if not cache_path.exists() and not excel_path.exists():
        lines.append(
            "\n(No hierarchy data yet — run the Hierarchy agent. Until then "
            "every contract reference will be tagged [STATUS UNKNOWN] and "
            "currency cannot be determined.)"
        )

    return "\n".join(lines)


def _walk_products(structure) -> list[str]:
    """Yield every non-null product name from a nested section_structure."""
    seen: list[str] = []
    if not structure: return seen
    for entry in structure:
        if not isinstance(entry, dict): continue
        p = entry.get("product")
        if isinstance(p, str) and p.strip():
            seen.append(p.strip())
        for sub in entry.get("subheaders") or []:
            if not isinstance(sub, dict): continue
            sp = sub.get("product")
            if isinstance(sp, str) and sp.strip():
                seen.append(sp.strip())
            for it in sub.get("items") or []:
                if isinstance(it, str) and it.strip():
                    seen.append(it.strip())
    return list(dict.fromkeys(seen))  # dedupe, preserve order


# ── Contract-status resolver (deterministic, hierarchy-based) ─────────────────
# Implements Levels 1 + 2 of the "active contract" awareness work:
#
#   Level 1 — tag every contract reference shown to the chat LLM with its
#             position in the amendment chain (ACTIVE / ROOT-PARTIAL /
#             SUPERSEDED / ORPHAN), and instruct the LLM to cite that tag
#             in its answer.
#   Level 2 — derive those tags DETERMINISTICALLY from the parent-child tree
#             written by the Hierarchy Agent. Works even when the LLM-supplied
#             is_active field is null, because leaf detection only needs the
#             Parent_Filename column.
#
# Detection rules (applied top-down, first match wins):
#   1. is_active == False in the cache       → SUPERSEDED   (explicit signal)
#   2. Hierarchy_Method == "orphan"          → ORPHAN       (no parent could
#                                                            be resolved;
#                                                            relationship to
#                                                            engagement unclear)
#   3. Filename has descendants in the tree:
#        - level == 0 or method=="root"/"miscellaneous"
#                                            → ROOT-PARTIAL (MSA base terms
#                                                            still apply where
#                                                            children don't
#                                                            override)
#        - otherwise                         → SUPERSEDED   (intermediate
#                                                            amendment that was
#                                                            itself later
#                                                            amended)
#   4. No descendants                        → ACTIVE       (leaf — terms here
#                                                            are currently in
#                                                            force)
#
# Sorting: every block that lists multiple contracts sorts ACTIVE first, then
# ROOT-PARTIAL, then ORPHAN, then SUPERSEDED. Within each status, newer
# Signed_Date first. This way the most-current sources reach the LLM at the
# top of each context block, where it pays the most attention.

_STATUS_PRIORITY = {
    "ACTIVE":       0,
    "ROOT-PARTIAL": 1,
    "ORPHAN":       2,
    "SUPERSEDED":   3,
    "UNKNOWN":      4,
}

_STATUS_LEGEND = (
    "=== CONTRACT-STATUS LEGEND — READ FIRST ===\n"
    "Every contract reference in the sections below is tagged with its position "
    "in the amendment chain. Use these tags when answering — and ALWAYS cite "
    "the source contract AND its status tag in your reply.\n"
    "  [ACTIVE]       — leaf of its amendment chain. No later document "
    "supersedes this one. Its fees, terms, and clauses are currently in force.\n"
    "  [ROOT-PARTIAL] — root agreement (MSA / base contract) that has at least "
    "one amendment below it. Its terms apply for anything NOT changed by a "
    "later amendment. For specific fees / products, prefer the [ACTIVE] "
    "amendment that touches them. Fall back to [ROOT-PARTIAL] for engagement-"
    "wide language like indemnification, governing law, notice, definitions.\n"
    "  [SUPERSEDED]   — an earlier document in the chain that a later "
    "amendment overrode. Do NOT quote its fees or scope as current. Still "
    "relevant for historical / transitional questions — if you cite it, say "
    "so explicitly.\n"
    "  [ORPHAN]       — the Hierarchy Agent could not link this contract to "
    "any parent in this client's corpus. Relationship to the wider engagement "
    "is unclear; treat its content with caution and flag this when citing.\n"
    "  [STATUS UNKNOWN] — Hierarchy Agent has not yet been run for this "
    "client, or the document was added after the last hierarchy pass.\n"
    "\nWHEN ANSWERING:\n"
    "  1. Prefer [ACTIVE] sources over [ROOT-PARTIAL]; [ROOT-PARTIAL] over "
    "[SUPERSEDED]; [SUPERSEDED] only as last resort.\n"
    "  2. If multiple [ACTIVE] contracts cover the same product / fee, pick "
    "the most recent by Signed_Date.\n"
    "  3. Always write the source like: \"From [ACTIVE] AmendmentNo3_2023.pdf "
    "(p.4): the ATM fee is $7.00/month.\" The status tag is part of the "
    "citation — never optional.\n"
    "  4. If you must fall back to [SUPERSEDED] or [ORPHAN], explicitly tell "
    "the user that the data may be outdated or unconfirmed.\n"
    "  5. If only [STATUS UNKNOWN] sources are available, advise the user to "
    "run the Hierarchy Agent so currency can be determined.\n"
)


def _get_contract_status(client_name: str) -> dict:
    """Return ``{filename: {status, level, parent, method, signed_date,
    is_active}}`` for every contract in this client's hierarchy.

    See the module-level comment block above for the detection rules. Returns
    ``{}`` when neither contracts_hierarchy.xlsx nor hierarchy_cache.json
    exists — callers should treat missing entries as [STATUS UNKNOWN]."""
    excel_path = OUTPUT_DIR / client_name / "contracts_hierarchy.xlsx"
    cache_path = OUTPUT_DIR / "hierarchy_cache.json"
    out: dict = {}

    # ── Read is_active from the cache (LLM-supplied, may be None) ────────────
    is_active_map: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            for key, meta in cache.items():
                if not isinstance(key, str) or not key.startswith(f"{client_name}/"):
                    continue
                fname = key.split("/", 1)[1]
                if isinstance(meta, dict):
                    is_active_map[fname] = meta.get("is_active")
        except Exception:
            pass

    if not excel_path.exists():
        # No hierarchy Excel yet — fall back to cache-only signal so we can
        # still honour an explicit is_active=False rather than show all as
        # [STATUS UNKNOWN].
        for fname, ia in is_active_map.items():
            if ia is False:
                status = "SUPERSEDED"
            elif ia is True:
                # Without the tree we can't confirm leaf-ness, but the LLM
                # explicitly said active — surface that rather than UNKNOWN.
                status = "ACTIVE"
            else:
                status = "UNKNOWN"
            out[fname] = {
                "status": status, "level": 0, "parent": "",
                "method": "", "signed_date": "", "is_active": ia,
            }
        return out

    try:
        df = pd.read_excel(str(excel_path))
    except Exception:
        return out
    if df.empty:
        return out

    # Resolve column names with the same camelCase / underscore fallback used
    # elsewhere in this module.
    def _col(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    # Column-name compatibility:
    #   • The updated standalone analyzer (Existing Scripts/contract_hierarchy_
    #     analyzer.py, May 2026+) writes Parent_Contract / Hierarchy_Status.
    #   • Earlier in-tree builds wrote Parent_Filename. Both are accepted.
    fname_col  = _col("Filename",         "filename")
    level_col  = _col("Hierarchy_Level",  "hierarchy_level")
    parent_col = _col("Parent_Contract",  "Parent_Filename",
                       "parent_contract", "parent_filename")
    method_col = _col("Hierarchy_Method", "hierarchy_method")
    date_col   = _col("Signed_Date",      "signed_date")
    status_col = _col("Hierarchy_Status", "hierarchy_status")    # "Child"/"Parent"/"Orphan"/…

    if not fname_col:
        return out

    # Build the "has children" set — any filename that appears as another
    # contract's Parent_Filename is by definition a parent. Pure tree
    # structure, no LLM involvement — works even when is_active is null.
    has_children: set = set()
    if parent_col:
        for _, row in df.iterrows():
            p = str(row.get(parent_col, "") or "").strip()
            if p and p.lower() not in ("nan", "none"):
                has_children.add(p)

    # Classify each row
    for _, row in df.iterrows():
        fname = str(row.get(fname_col, "")).strip()
        if not fname or fname.lower() == "nan":
            continue
        try:
            level = int(row.get(level_col, 0) or 0) if level_col else 0
        except (TypeError, ValueError):
            level = 0
        parent = str(row.get(parent_col, "") or "").strip() if parent_col else ""
        if parent.lower() in ("nan", "none", "-"):
            parent = ""
        method = str(row.get(method_col, "") or "").strip().lower() if method_col else ""
        sdate  = str(row.get(date_col, "") or "").strip() if date_col else ""
        if sdate.lower() in ("nan", "none"):
            sdate = ""
        # Hierarchy_Status carries the analyzer's own label: "Parent" / "Child"
        # / "Orphan" / "Standalone" / "Miscellaneous" / "Duplicate". Use it as
        # the strongest signal — it already encodes the analyzer's verdict
        # after running its full 5-strategy resolver.
        h_status_raw = (str(row.get(status_col, "")).strip().lower()
                        if status_col else "")
        is_active = is_active_map.get(fname)

        # Classification — order matters (most-specific signal first):
        #   1. Explicit is_active=False in cache → SUPERSEDED
        #   2. Hierarchy_Status column from analyzer (most direct signal):
        #        "orphan"       → ORPHAN
        #        "parent"       → ROOT-PARTIAL (has children — the analyzer
        #                          already confirmed it; trust it over our
        #                          inferred has_children set)
        #        "duplicate"    → SUPERSEDED (canonical copy lives elsewhere)
        #        "standalone" / "miscellaneous"
        #                       → ACTIVE (root-eligible non-MSA with no parent
        #                          AND no children in this corpus; treat as a
        #                          standalone in-force document)
        #        "child"        → leaf? has_children check decides
        #   3. Fall through to the parent-child inference from the tree.
        if is_active is False:
            status = "SUPERSEDED"
        elif h_status_raw == "orphan" or method == "orphan":
            status = "ORPHAN"
        elif h_status_raw in ("parent",) or fname in has_children:
            # Has descendants — root vs intermediate determines partial vs
            # full supersede.
            if level == 0 or method in ("root", "miscellaneous"):
                status = "ROOT-PARTIAL"
            else:
                status = "SUPERSEDED"
        elif h_status_raw == "duplicate":
            status = "SUPERSEDED"
        elif h_status_raw in ("standalone", "miscellaneous"):
            status = "ACTIVE"
        else:
            # Leaf of its chain — currently in force
            status = "ACTIVE"

        out[fname] = {
            "status":      status,
            "level":       level,
            "parent":      parent,
            "method":      method,
            "signed_date": sdate,
            "is_active":   is_active,
        }
    return out


def _status_tag(fname: str, status_map: dict) -> str:
    """Compact tag like '[ACTIVE]' or '[STATUS UNKNOWN]'. Safe for unknown names."""
    info = status_map.get(fname)
    if not info:
        return "[STATUS UNKNOWN]"
    return f"[{info['status']}]"


def _invert_date_for_desc(date_str: str) -> str:
    """Map 'YYYY-MM-DD' to a string where ASCENDING sort yields newest first.
    9's-complement on every digit; non-digits pass through. Missing/garbage
    dates return a string that sorts AFTER any real date so they fall to the
    bottom of each status group."""
    if not date_str or len(date_str) < 4 or not date_str[:4].isdigit():
        return "~~~~~~~~~~"
    s = date_str[:10]
    return "".join(str(9 - int(c)) if c.isdigit() else c for c in s)


def _sort_filenames_by_status(filenames, status_map: dict) -> list:
    """Sort filenames ACTIVE-first, then by Signed_Date descending within
    each status. Used by every multi-contract context block so the most-
    current sources reach the LLM at the top of each section."""
    def sort_key(fname):
        info = status_map.get(fname) or {}
        status = info.get("status", "UNKNOWN")
        sdate  = info.get("signed_date") or ""
        return (
            _STATUS_PRIORITY.get(status, 9),
            _invert_date_for_desc(sdate),
            str(fname),                  # tiebreak — stable order across runs
        )
    return sorted(filenames, key=sort_key)


# ── Extraction ─────────────────────────────────────────────────────────────────

def _load_material_codes(client_name: str) -> dict:
    """Return {(Source Contract, Item, Price): (Material Code, Matched
    Description)} so build_extraction_context can annotate matched items.

    System of record: prefers validated_material_output.xlsx (Material
    Validation Agent) when present, mapping its new_material_code /
    new_matched_description and falling back per-row to fallback_material_code
    when the validated code is blank. Falls back to material_match_output.xlsx
    (Matching Agent) when validation hasn't run — so behaviour is unchanged
    wherever the validation agent couldn't run (e.g. off the Snowflake VDI)."""
    base      = OUTPUT_DIR / client_name
    validated = base / "validated_material_output.xlsx"
    legacy    = base / "material_match_output.xlsx"
    p = validated if validated.exists() else legacy
    if not p.exists():
        return {}
    try:
        mm = pd.read_excel(str(p))
    except Exception:
        return {}
    if mm.empty:
        return {}

    is_validated = (p == validated)
    code_col = "new_material_code"       if is_validated else "Material Code"
    desc_col = "new_matched_description" if is_validated else "Matched Description"
    # Validated file present but unexpectedly missing its columns → fall back.
    if code_col not in mm.columns:
        if is_validated and legacy.exists():
            try:
                mm = pd.read_excel(str(legacy))
            except Exception:
                return {}
            is_validated = False
            code_col, desc_col = "Material Code", "Matched Description"
        if mm.empty or code_col not in mm.columns:
            return {}

    out: dict = {}
    for _, row in mm.iterrows():
        key = (str(row.get("Source Contract", "")), str(row.get("Item", "")),
               str(row.get("Price", "")))
        mc = str(row.get(code_col, "") or "").strip()
        if is_validated and (not mc or mc.lower() == "nan"):
            mc = str(row.get("fallback_material_code", "") or "").strip()
        if mc.lower() == "nan":
            mc = ""
        md = str(row.get(desc_col, "") or "").strip()
        if md.lower() == "nan":
            md = ""
        out[key] = (mc, md)
    return out


def build_extraction_context(client_name: str) -> str:
    """
    Load extracted line items and return a readable summary.
    Groups by source contract; includes item description, price, fee billing
    term, and the matched material code (when material_match_output.xlsx is
    present alongside).
    """
    excel_path = OUTPUT_DIR / client_name / "extraction_output.xlsx"
    if not excel_path.exists():
        return "(No extraction data — run the Fee Description Agent first)"

    try:
        df = pd.read_excel(str(excel_path))
    except Exception as e:
        return f"(Could not read extraction output: {e})"

    if df.empty:
        return "(Extraction ran but found no items)"

    # Pull material codes from the dedicated matching output (separate file
    # since the Material Code Matching Agent was split off).
    code_lookup = _load_material_codes(client_name)

    # Read the new "Fee Billing Term" column with a fallback to the legacy
    # "Pricing_Condition" so older pre-rename outputs still render correctly.
    term_col = ("Fee Billing Term" if "Fee Billing Term" in df.columns
                else ("Pricing_Condition" if "Pricing_Condition" in df.columns else None))

    # Pull the hierarchy status map so each source-contract group can be tagged
    # [ACTIVE] / [SUPERSEDED] / [ROOT-PARTIAL] / [ORPHAN]. Groups are sorted
    # ACTIVE-first below so the LLM sees current pricing before historical
    # pricing.
    status_map = _get_contract_status(client_name)

    lines = [
        f"=== EXTRACTED LINE ITEMS ({len(df)} items) ===",
        "Each source contract is tagged with its hierarchy status. When a fee "
        "for the same item appears under multiple contracts, USE THE [ACTIVE] "
        "ONE — it reflects the price currently in force. See the "
        "CONTRACT-STATUS LEGEND in the hierarchy block for the full meaning "
        "of each tag.",
    ]
    _CB_TRUE = {"true", "1", "1.0"}

    # Sort source contracts ACTIVE-first, newer-first within each status.
    source_order = _sort_filenames_by_status(
        [s for s in df["Source Contract"].astype(str).unique() if s and s.lower() != "nan"],
        status_map,
    )
    for source in source_order:
        grp = df[df["Source Contract"].astype(str) == source]
        if grp.empty:
            continue
        date_str = str(grp["Date"].iloc[0]) if "Date" in grp.columns else ""
        tag      = _status_tag(source, status_map)
        lines.append(f"\n— {tag} {source}" + (f" ({date_str})" if date_str else ""))

        # Pre-aggregate every page number that has at least one extracted
        # item under this contract, then surface it ON ITS OWN LINE right
        # below the contract header. The per-item (p.N) tags below are
        # still there, but giving the LLM a ready-made comma-separated
        # set means it can lift the citation verbatim into Sources
        # instead of having to scan and dedupe across the item rows.
        # This was the #1 reason page numbers stopped showing up in
        # answers — the data was correct but the LLM was being lazy
        # about the aggregation step.
        _pages_in_grp = sorted({
            int(_format_page(p))
            for p in grp.get("Page", [])
            if _format_page(p).isdigit()
        })
        if _pages_in_grp:
            _pp = ", ".join(f"p.{n}" for n in _pages_in_grp)
            lines.append(f"  PAGES with extracted items: [{_pp}]  "
                         f"← cite these EXACTLY when sourcing this contract")

        for _, row in grp.iterrows():
            item    = str(row.get("Item", "?"))[:90]
            # For textual fees the cleaned-price will be blank; fall back to raw Price.
            cleaned = str(row.get("Cleaned Price", "") or "").strip()
            raw_p   = str(row.get("Price", "") or "").strip()
            price   = cleaned if cleaned and cleaned != "nan" else raw_p or "?"
            ftype   = str(row.get("Fee Type", "") or "").strip()
            cb      = row.get("Checkbox_Checked")
            section = str(row.get("Section_Header", "") or "")
            page    = _format_page(row.get("Page"))
            term    = str(row.get(term_col, "") if term_col else "" or "").strip()

            # Material code (joined from material_match_output.xlsx). Falls
            # back to any code/Matched-Description columns that might still
            # live in extraction_output.xlsx from an older pipeline.
            key = (str(source), str(row.get("Item", "")), str(row.get("Price", "")))
            mc, mdesc = code_lookup.get(key, ("", ""))
            if not mc:
                mc    = str(row.get("Material Code", "") or "")
                mdesc = str(row.get("Matched Description", "") or "")

            cb_str   = "" if cb is None or str(cb) == "nan" else \
                       " ✓" if str(cb).lower() in _CB_TRUE else " ✗"
            type_str = f" <{ftype}>" if ftype and ftype not in ("", "dollar", "nan") else ""
            code_str = (f" [{mc}]" if mc and mc.lower() != "nan"
                        else (f" [{mdesc[:40]}]" if mdesc and mdesc.lower() != "nan" else ""))
            sec_str  = f"  §{section[:50]}" if section and section != "nan" else ""
            page_str = f"  (p.{page})" if page else ""
            term_str = f"  Bill term: {term[:80]}" if term and term.lower() != "nan" else ""

            lines.append(f"  {item}{cb_str}  {price}{type_str}{code_str}{sec_str}{page_str}{term_str}")

    return "\n".join(lines)


# ── Engagement Overview (renamed from Master Contract scope) ─────────────────

def build_engagement_overview_context(client_name: str) -> str:
    """Format engagement_overview_output.xlsx — signatures, addresses, contract
    summaries, and (for SOWs) project metadata. Falls back to the legacy
    master_contract_output.xlsx path for back-compat with older runs."""
    p = OUTPUT_DIR / client_name / "engagement_overview_output.xlsx"
    if not p.exists():
        legacy = OUTPUT_DIR / client_name / "master_contract_output.xlsx"
        if legacy.exists():
            p = legacy
        else:
            return ""
    try:
        df = pd.read_excel(str(p))
    except Exception as e:
        return f"(Engagement Overview: could not load — {e})"
    if df.empty:
        return ""

    # Tag each engagement-overview row with its hierarchy status, and sort
    # rows ACTIVE-first so currently-in-force engagement metadata leads.
    status_map = _get_contract_status(client_name)
    df = df.copy()
    # Defensive: pandas' DataFrame.get(key, default) returns the *default*
    # value (here a bare empty string) when the column is missing — and
    # `str` has no `.astype()`. Older / partially-populated client outputs
    # may not have a "Filename" column at all, so we guard with a column
    # check before delegating to Series.astype.
    df["_fname_str"] = (
        df["Filename"].astype(str)
        if "Filename" in df.columns
        else ""
    )
    fname_order = _sort_filenames_by_status(
        [f for f in df["_fname_str"].unique() if f and f.lower() != "nan"],
        status_map,
    )
    df_by_fname = {f: g for f, g in df.groupby("_fname_str", sort=False)}

    lines = [f"=== ENGAGEMENT OVERVIEW ({len(df)} contracts) ==="]
    for fname_key in fname_order:
        grp = df_by_fname.get(fname_key)
        if grp is None or grp.empty:
            continue
        row    = grp.iloc[0]
        fname  = str(row.get("Filename", ""))[:70]
        ctype  = str(row.get("Contract Type", "") or "")
        dtype  = str(row.get("Document Type", "") or "").strip()
        date   = str(row.get("Contract Effective Date", "")).replace("_", "-")
        c_addr = str(row.get("Client Address", "") or "").strip()
        p_addr = str(row.get("Provider Address", "") or "").strip()
        c_sig  = " | ".join(filter(None, [
            str(row.get("Client Signatory Name",  "") or "").strip(),
            str(row.get("Client Signatory Title", "") or "").strip(),
            str(row.get("Client Signatory Date",  "") or "").strip(),
        ]))
        p_sig  = " | ".join(filter(None, [
            str(row.get("Provider Signatory Name",  "") or "").strip(),
            str(row.get("Provider Signatory Title", "") or "").strip(),
            str(row.get("Provider Signatory Date",  "") or "").strip(),
        ]))
        summary = str(row.get("Contract Summary", "") or "").strip()

        header_bits = [ctype]
        if dtype and dtype.lower() not in ("nan", ""):
            header_bits.append(f"doc_type={dtype}")
        tag = _status_tag(fname_key, status_map)
        lines.append(f"\n{tag} [{' / '.join(b for b in header_bits if b)}] {fname} ({date})")
        if c_addr and c_addr.lower() != "nan":
            lines.append(f"  Client address: {c_addr[:160]}")
        if p_addr and p_addr.lower() != "nan":
            lines.append(f"  Provider address: {p_addr[:160]}")
        if c_sig.strip(" |"):
            lines.append(f"  Client signed: {c_sig}")
        if p_sig.strip(" |"):
            lines.append(f"  Provider signed: {p_sig}")
        if summary and summary.lower() != "nan":
            lines.append(f"  Summary: {summary[:400]}")
    return "\n".join(lines)


# ── Product Hierarchy (Phase 2) ───────────────────────────────────────────────
# DISTINCT from build_hierarchy_context above. build_hierarchy_context renders
# the CONTRACT hierarchy (which document amends which master); this one renders
# the PRODUCT hierarchy WITHIN each contract (Parent → Level → Product → Module
# rows from agents/master_contract.py's Phase 2 output). Surfacing them as
# separate sections in the chat prompt prevents the model from conflating the
# two when the user asks about "product hierarchy".

def build_product_hierarchy_context(client_name: str) -> str:
    """Format product_hierarchy_output.xlsx — products & modules per contract."""
    p = OUTPUT_DIR / client_name / "product_hierarchy_output.xlsx"
    if not p.exists():
        return ""
    try:
        df = pd.read_excel(str(p))
    except Exception as e:
        return f"(Product Hierarchy: could not load — {e})"
    if df.empty:
        return ""

    # Drop empty placeholder rows (added when Phase 2 produced no modules)
    df = df.copy()
    df["Product"] = df["Product"].astype(str).fillna("").str.strip()
    df = df[df["Product"] != ""]
    if df.empty:
        return ""

    # Pull hierarchy status so each contract block can be tagged and the
    # blocks sorted ACTIVE-first. When the user asks "what products does
    # this engagement cover", the LLM should see currently-in-force product
    # lists before superseded ones.
    status_map = _get_contract_status(client_name)

    lines = [
        f"=== PRODUCT HIERARCHY (Phase 2) ({len(df)} rows across "
        f"{df['Filename'].nunique()} contracts) ===",
        "Note: this is the product/module hierarchy WITHIN each contract "
        "(Parent → Level → Product → Module). It is SEPARATE from the "
        "contract-hierarchy tree (which contract amends which master) "
        "shown above. When the user asks about 'product hierarchy' or "
        "'modules' or 'what services does this contract cover', use the "
        "data in this section — and prefer the [ACTIVE] contract for "
        "current-state questions.",
    ]
    # Sort contracts ACTIVE-first so the most-current product lists lead.
    fname_order = _sort_filenames_by_status(
        [f for f in df["Filename"].astype(str).unique() if f and f.lower() != "nan"],
        status_map,
    )
    for fname in fname_order:
        grp = df[df["Filename"].astype(str) == fname]
        if grp.empty:
            continue
        dtype = str(grp["Document Type"].iloc[0] or "").strip() if "Document Type" in grp.columns else ""
        ctype = str(grp["Contract Type"].iloc[0] or "").strip() if "Contract Type" in grp.columns else ""
        hdr   = " / ".join(b for b in (ctype, f"doc_type={dtype}" if dtype else "") if b)
        tag   = _status_tag(fname, status_map)
        lines.append(f"\n— {tag} {fname}" + (f"  [{hdr}]" if hdr else ""))
        for _, row in grp.iterrows():
            par  = str(row.get("Parent",  "") or "").strip()
            lvl  = str(row.get("Level",   "") or "").strip()
            prod = str(row.get("Product", "") or "").strip()
            mod  = str(row.get("Module",  "") or "").strip()
            mod_str = f" → {mod}" if mod else ""
            lines.append(f"    {par} · {lvl} · {prod}{mod_str}")
    return "\n".join(lines)


# ── Generic clause-extractor context ──────────────────────────────────────────

# Each clause-extractor agent writes <name>_output.xlsx in Output/<Client>/.
# We surface only the non-empty rows (where the LLM actually extracted something)
# so the chat prompt isn't padded with blank placeholders.

_CLAUSE_AGENTS = [
    # (filename_base, display_label, content_cols_to_check)
    ("term_renewal",  "TERM & RENEWAL",
     ["Initial Term", "Renewal Period", "Auto-Renew",
      "Notice to Non-Renew", "Expiration / End Date"]),
    ("termination",   "TERMINATION",
     ["Termination for Cause", "Termination for Convenience",
      "Early Termination Fee", "Notice Period", "Survival Clauses"]),
    ("sla",           "SLA & SERVICE CREDITS",
     ["Uptime Commitment", "Service Credit Formula",
      "Response Time", "Resolution Time", "Covered Services"]),
    ("volume_tiers",  "VOLUME TIERS & MINIMUMS",
     ["Minimum Commitment", "Volume Tiers", "Tier Basis",
      "True-Up Cadence", "Overage Charges"]),
]


def build_clause_context(client_name: str, agent_name: str,
                          display: str, content_cols: list) -> str:
    """Format one clause-extractor's xlsx into chat-ready text. Returns '' if no data."""
    p = OUTPUT_DIR / client_name / f"{agent_name}_output.xlsx"
    if not p.exists():
        return ""
    try:
        df = pd.read_excel(str(p))
    except Exception as e:
        return f"({display}: could not load — {e})"
    if df.empty:
        return ""

    # Only keep rows where at least one of the content columns has a value.
    cols_present = [c for c in content_cols if c in df.columns]
    if not cols_present:
        return ""
    has_data = df[cols_present].astype(str).apply(
        lambda s: s.str.strip().replace({"nan": ""}) != "", axis=0).any(axis=1)
    df = df[has_data]
    if df.empty:
        return ""

    # Tag each clause row with its hierarchy status and sort ACTIVE-first so
    # the LLM sees currently-binding clauses before superseded ones.
    status_map = _get_contract_status(client_name)
    df = df.copy().reset_index(drop=True)
    # Defensive: pandas' DataFrame.get(key, default) returns the *default*
    # value (here a bare empty string) when the column is missing — and
    # `str` has no `.astype()`. Older / partially-populated client outputs
    # may not have a "Filename" column at all, so we guard with a column
    # check before delegating to Series.astype.
    df["_fname_str"] = (
        df["Filename"].astype(str)
        if "Filename" in df.columns
        else ""
    )
    fname_order = _sort_filenames_by_status(
        [f for f in df["_fname_str"].unique() if f and f.lower() != "nan"],
        status_map,
    )
    # Preserve any rows with no usable Filename at the end (rare; flags as
    # UNKNOWN by virtue of the empty status_map lookup).
    seen_keys = set(fname_order)
    extras = [f for f in df["_fname_str"].unique() if f not in seen_keys]
    fname_order = fname_order + extras

    lines = [f"=== {display} ({len(df)} contracts) ==="]
    for fname_key in fname_order:
        grp = df[df["_fname_str"] == fname_key]
        if grp.empty:
            continue
        for _, row in grp.iterrows():
            fname = str(row.get("Filename", ""))[:70]
            date  = str(row.get("Contract Effective Date", "")).replace("_", "-")
            ctype = str(row.get("Contract Type", ""))
            page  = _format_page(row.get("Page Number"))
            page_tag = f" (p.{page})" if page else ""
            tag      = _status_tag(fname_key, status_map)
            lines.append(f"\n{tag} [{ctype}] {fname} ({date}){page_tag}")
            for col in cols_present:
                val = str(row.get(col, "") or "").strip()
                if val and val.lower() != "nan":
                    lines.append(f"  {col}: {val[:200]}")
            # Source snippet, when present, helps the LLM ground its answer
            snippet = str(row.get("Source Snippet", "") or "").strip()
            if snippet and snippet.lower() != "nan":
                lines.append(f"  Source quote{page_tag}: \"{snippet[:240]}\"")
    return "\n".join(lines)


# ── CPI ────────────────────────────────────────────────────────────────────────

def build_cpi_context(client_name: str) -> str:
    """Load CPI output and return a readable summary."""
    cpi_path = OUTPUT_DIR / client_name / "cpi_output.xlsx"
    if not cpi_path.exists():
        return "(No CPI data — upload a CPI matches file and run the CPI agent)"

    try:
        df = pd.read_excel(str(cpi_path))
    except Exception as e:
        return f"(Could not read CPI output: {e})"

    if df.empty:
        return "(CPI file exists but contains no records)"

    # Tag each CPI record with the source contract's hierarchy status and
    # sort ACTIVE-first so the LLM sees current escalation terms before
    # superseded ones.
    status_map = _get_contract_status(client_name)
    df = df.copy().reset_index(drop=True)
    # Defensive: pandas' DataFrame.get(key, default) returns the *default*
    # value (here a bare empty string) when the column is missing — and
    # `str` has no `.astype()`. Older / partially-populated client outputs
    # may not have a "Filename" column at all, so we guard with a column
    # check before delegating to Series.astype.
    df["_fname_str"] = (
        df["Filename"].astype(str)
        if "Filename" in df.columns
        else ""
    )
    fname_order = _sort_filenames_by_status(
        [f for f in df["_fname_str"].unique() if f and f.lower() != "nan"],
        status_map,
    )
    seen_keys = set(fname_order)
    extras = [f for f in df["_fname_str"].unique() if f not in seen_keys]
    fname_order = fname_order + extras

    lines = [f"=== CPI ESCALATION TERMS ({len(df)} records) ==="]
    for fname_key in fname_order:
        grp = df[df["_fname_str"] == fname_key]
        if grp.empty:
            continue
        for _, row in grp.iterrows():
            ctype   = str(row.get("Contract Type", "?"))
            eff     = str(row.get("Contract Effective Date", "?"))
            fname   = str(row.get("Filename", "") or "").strip()
            terms   = str(row.get("CPI Terms (per Contract)", "?"))
            yr      = str(row.get("CPI Eligibility Year", "") or "")
            mo      = str(row.get("CPI Eligibility Month", "") or "")
            notice  = str(row.get("Notice Requirement", "") or "")
            snippet = str(row.get("Contract Language/Information", "") or "")
            page    = _format_page(row.get("Page Number"))
            page_tag = f" (p.{page})" if page else ""

            # Filename header makes citations possible — the LLM needs to know
            # which contract this record belongs to. Lead with the status tag
            # so the LLM picks the currently-in-force CPI terms first.
            tag    = _status_tag(fname_key, status_map)
            header = f"\n{tag} [{ctype}]"
            if fname and fname.lower() != "nan":
                header += f" {fname[:70]}"
            header += f" Effective: {eff}{page_tag}"
            lines.append(header)
            lines.append(
                f"  Terms: {terms}"
                + (f"  |  Eligible: {mo} {yr}".rstrip() if (mo or yr) else "")
                + (f"  |  Notice: {notice}" if notice else "")
            )
            if snippet and snippet not in ("", "nan"):
                lines.append(f"  Language{page_tag}: \"{snippet[:200]}\"")

    return "\n".join(lines)


# ── Master builder ─────────────────────────────────────────────────────────────

def build_full_context(client_name: str) -> dict[str, str]:
    """
    Return all context strings for a single client (KB + 4 source streams).
    Safe to call even if agents haven't been run yet (returns placeholder text).
    """
    clause_parts = []
    for agent_name, display, cols in _CLAUSE_AGENTS:
        block = build_clause_context(client_name, agent_name, display, cols)
        if block:
            clause_parts.append(block)
    return {
        "kb":                load_kb_sections(),
        "hierarchy":         build_hierarchy_context(client_name),
        "master_contract":   build_engagement_overview_context(client_name)
                             or "(No engagement-overview data yet)",
        "product_hierarchy": build_product_hierarchy_context(client_name)
                             or "(No product-hierarchy data — run the Product Module Agent)",
        "extraction":        build_extraction_context(client_name),
        "cpi":               build_cpi_context(client_name),
        "clauses":           "\n\n".join(clause_parts) if clause_parts
                              else "(No clause data — run the clause extractors)",
    }


def build_multi_client_context(client_names: list[str]) -> dict[str, str]:
    """
    Aggregate context strings across multiple clients.
    Each client's section is clearly labelled; clients with no data are omitted.
    """
    if not client_names:
        return {"kb": "", "hierarchy": "", "master_contract": "",
                "product_hierarchy": "", "extraction": "", "cpi": "",
                "clauses": ""}
    if len(client_names) == 1:
        return build_full_context(client_names[0])

    kb = load_kb_sections()

    def _sep(name: str) -> str:
        return f"\n\n{'━'*52}\nCLIENT: {name}\n{'━'*52}\n"

    hier_parts:    list[str] = []
    master_parts:  list[str] = []
    product_parts: list[str] = []
    extr_parts:    list[str] = []
    cpi_parts:     list[str] = []
    clauses_parts: list[str] = []

    for name in client_names:
        h = build_hierarchy_context(name)
        if not h.startswith("(No"):
            hier_parts.append(_sep(name) + h)

        m = build_engagement_overview_context(name)
        if m:
            master_parts.append(_sep(name) + m)

        ph = build_product_hierarchy_context(name)
        if ph:
            product_parts.append(_sep(name) + ph)

        e = build_extraction_context(name)
        if not e.startswith("(No"):
            extr_parts.append(_sep(name) + e)

        c = build_cpi_context(name)
        if not c.startswith("(No"):
            cpi_parts.append(_sep(name) + c)

        # Clause extractors — aggregate the 4 types per client.
        per_client = []
        for agent_name, display, cols in _CLAUSE_AGENTS:
            block = build_clause_context(name, agent_name, display, cols)
            if block:
                per_client.append(block)
        if per_client:
            clauses_parts.append(_sep(name) + "\n\n".join(per_client))

    return {
        "kb":                kb,
        "hierarchy":         "".join(hier_parts)    or "(No hierarchy data — load clients first)",
        "master_contract":   "".join(master_parts)  or "(No master-contract scope data yet)",
        "product_hierarchy": "".join(product_parts) or "(No product-hierarchy data — re-run the Master Contract agent)",
        "extraction":        "".join(extr_parts)    or "(No extraction data — run Extraction agent)",
        "cpi":               "".join(cpi_parts)     or "(No CPI data)",
        "clauses":           "".join(clauses_parts) or "(No clause data — run the clause extractors)",
    }
