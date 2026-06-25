"""
aggregated_retrieval.py — uncapped, aggregated invoice retrieval for the
Contract Intelligence chatbot.
=========================================================================

This module replaces the raw-row path in snowflake_invoice.fetch_invoice_lines()
for questions about HISTORICAL MATERIAL CODES (one-time fees, hardware,
implementation charges that get buried under thousands of recurring monthly
SaaS lines).

The failure mode it solves:

    Old path  → SELECT ... ORDER BY invoice_date DESC LIMIT 400
                → 400 recent recurring rows fill the window
                → 2019 one-time hardware codes never make it into the LLM context
                → chatbot says "no record of code FD306-HW-INSTALL on this contract"

    New path  → GROUP BY material_code, item_category — one row per code
                → COUNT(*), MIN/MAX(invoice_date), SUM(net), distinct months
                → NO LIMIT (aggregation collapses 50,000 lines to ~200 rows)
                → every code ever billed is in the payload, with first/last dates

Four-step pipeline (`build_aggregated_context()`):

    1. extract_filters()       — pull contract_id / client_name / date_range
    2. build_aggregated_sql()  — render the GROUP BY template + bind params
    3. execute_aggregated()    — read-only session, returns DataFrame
    4. render_markdown_payload + SYNTHESIS_SYSTEM_PROMPT
                               — emit the prompt block for the final LLM

Designed to be a drop-in alongside snowflake_invoice.build_invoice_context();
the chatbot routes "material codes ever billed" questions here and keeps the
existing rendering for "show me the latest 5 invoices" type questions where
per-line detail still matters.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from snowflake_invoice import (
    _agent_material_map,
    _discover_columns,
    _get_connection,
    _load_cfg,
    _qualified_table,
    resolve_billto,
    snowflake_available,
)


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — PARAMETER & ENTITY EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────
#
# Deterministic regex extractor first; an LLM fallback hook is provided for the
# 5–10% of questions where the contract reference is written in prose
# ("the FD306 master agreement", "their 2018 SaaS contract"). Regex catches the
# common, structured cases without an LLM round-trip — fast and free.

_CONTRACT_ID_RE = re.compile(
    r"\b("
    r"CTR[-_ ]?\d{2,6}"       # CTR-992, CTR_001
    r"|MSA[-_ ]?\d{2,6}"      # MSA-12
    r"|MA[-_ ]?\d{2,6}"       # MA-77
    r"|SOW[-_ ]?\d{2,6}"      # SOW-3
    r"|FD\d{3,4}"             # FD306, FD1024  (Fiserv internal IDs)
    r")\b",
    re.IGNORECASE,
)

# "since inception", "all-time", "lifetime" → unbounded date range
_INCEPTION_RE = re.compile(
    r"\b(since\s+inception|all[\s-]?time|lifetime|to\s+date|"
    r"ever\s+billed|since\s+the\s+(?:start|beginning))\b",
    re.IGNORECASE,
)

# ISO and US date forms inside a "between X and Y" or "from X to Y" or "since X"
_DATE_RE = re.compile(
    r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4})\b"
)
_SINCE_RE = re.compile(r"\bsince\s+(\d{4})\b", re.IGNORECASE)


@dataclass
class Filters:
    """Structured query parameters extracted from a NL question.

    `contract_ids` is the user-facing identifier (CTR-992); `billto_codes`
    is what actually goes into the SQL WHERE clause (resolved in step 2).
    `material_codes` is a *narrowing* filter applied when the user names
    specific codes; left empty it means "every code billed".
    """
    client_name: Optional[str] = None
    contract_ids: list[str] = field(default_factory=list)
    billto_codes: list[str] = field(default_factory=list)
    material_codes: list[str] = field(default_factory=list)
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    # Reason this extraction picked these values — surfaced in the prompt
    # so the LLM can tell the user how the question was interpreted.
    notes: list[str] = field(default_factory=list)


def extract_filters(question: str, focused_client: Optional[str] = None) -> Filters:
    """Step 1. Pull structured filters out of a natural-language question.

    `focused_client` is the chatbot's currently-selected client folder name;
    used as the default when the question doesn't name a client explicitly
    (which is the normal case — the user has already clicked into a client
    in the UI before asking about contracts).
    """
    f = Filters(client_name=focused_client)
    if not question:
        return f

    for m in _CONTRACT_ID_RE.findall(question):
        cid = re.sub(r"[\s_]+", "-", m.upper())
        if cid not in f.contract_ids:
            f.contract_ids.append(cid)
    if f.contract_ids:
        f.notes.append(f"contract_ids = {f.contract_ids}")

    if _INCEPTION_RE.search(question):
        f.date_from = None
        f.date_to = None
        f.notes.append("date_range = inception → today (unbounded)")
    else:
        dates = _parse_dates(question)
        if dates:
            f.date_from, f.date_to = dates
            f.notes.append(f"date_range = {f.date_from} → {f.date_to or 'today'}")
        else:
            m = _SINCE_RE.search(question)
            if m:
                f.date_from = date(int(m.group(1)), 1, 1)
                f.notes.append(f"date_range = {f.date_from} → today")

    return f


def _parse_dates(q: str) -> Optional[tuple[Optional[date], Optional[date]]]:
    """Parse 'between X and Y' / 'from X to Y' / 'since X' constructions."""
    found = _DATE_RE.findall(q)
    if not found:
        return None
    parsed = [_to_date(d) for d in found]
    parsed = [d for d in parsed if d]
    if not parsed:
        return None
    if len(parsed) == 1:
        return (parsed[0], None)
    return (min(parsed), max(parsed))


def _to_date(s: str) -> Optional[date]:
    s = s.replace("/", "-")
    parts = s.split("-")
    try:
        if len(parts[0]) == 4:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        else:
            m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
        return date(y, m, d)
    except (ValueError, IndexError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — AGGREGATED SQL GENERATION
# ─────────────────────────────────────────────────────────────────────────────
#
# The whole point of this module: collapse N invoice lines per (code, category)
# pair into ONE row with rollup aggregates BEFORE the data crosses into the
# LLM's context. A material code billed 50 times occupies one row, not 50.
#
# Identifiers (table, columns) are validated in snowflake_invoice._qualified_table
# and the columns list comes from the canonical INVOICE_COLUMNS — never from
# user input — so the f-string interpolation here is safe. All user-supplied
# values (bill-to codes, dates, material codes) are passed as %s binds.

AGGREGATED_SQL_TEMPLATE = """
SELECT
    OTC_SIL_MATERIAL                                              AS material_code,
    ANY_VALUE(OTC_SIL_MATERIAL_TEXT)                              AS material_description,
    OTC_SIL_ITEM_CATEGORY                                         AS item_category,
    ANY_VALUE(OTC_SIL_MAT_ASSIGN_GL_CATEGORY)                     AS gl_category,
    ANY_VALUE(OTC_SIL_MAT_ASSIGN_GL_ACCOUNTDESCRIP)               AS gl_account_desc,
    ANY_VALUE(OTC_SIL_PRODUCT_HIER_NAME)                          AS product_hierarchy,
    COUNT(*)                                                      AS line_count,
    COUNT(DISTINCT OTC_SIH_INVOICE_DOCUMENT)                      AS invoice_doc_count,
    COUNT(DISTINCT DATE_TRUNC('MONTH', OTC_SIH_INVOICE_DATE))     AS distinct_months,
    MIN(OTC_SIH_INVOICE_DATE)                                     AS first_billed,
    MAX(OTC_SIH_INVOICE_DATE)                                     AS last_billed,
    SUM(TRY_TO_DECIMAL(OTC_SIL_NET_AMOUNT::STRING, 38, 4))        AS total_net,
    SUM(TRY_TO_DECIMAL(OTC_SIL_TAX_AMOUNT::STRING, 38, 4))        AS total_tax,
    SUM(TRY_TO_DECIMAL(OTC_SIL_ORDER_QUANTITY::STRING, 38, 4))    AS total_quantity,
    MIN(TRY_TO_DECIMAL(OTC_SIL_NET_AMOUNT::STRING, 38, 4))        AS min_net,
    MAX(TRY_TO_DECIMAL(OTC_SIL_NET_AMOUNT::STRING, 38, 4))        AS max_net,
    CASE
        WHEN COUNT(DISTINCT DATE_TRUNC('MONTH', OTC_SIH_INVOICE_DATE)) >= 6
            THEN 'RECURRING'
        WHEN COUNT(DISTINCT DATE_TRUNC('MONTH', OTC_SIH_INVOICE_DATE)) BETWEEN 2 AND 5
            THEN 'PERIODIC'
        ELSE 'ONE_TIME'
    END                                                           AS billing_type
FROM {qualified_table}
WHERE OTC_SIH_BILLTO IN ({billto_placeholders})
  AND OTC_SIL_MATERIAL IS NOT NULL
  AND TRIM(OTC_SIL_MATERIAL) <> ''
  {material_filter}
  {date_from_filter}
  {date_to_filter}
GROUP BY
    OTC_SIL_MATERIAL,
    OTC_SIL_ITEM_CATEGORY
ORDER BY
    CASE billing_type
        WHEN 'ONE_TIME'  THEN 1
        WHEN 'PERIODIC'  THEN 2
        WHEN 'RECURRING' THEN 3
    END,
    first_billed ASC,
    total_net DESC
""".strip()


def build_aggregated_sql(f: Filters, cfg: dict) -> tuple[str, list]:
    """Render the GROUP BY template against the resolved schema. Returns
    (sql, bind_params). Caller must have populated `f.billto_codes` already."""
    if not f.billto_codes:
        raise ValueError("Filters.billto_codes is empty — nothing to query.")

    qt = _qualified_table(cfg)
    billto_placeholders = ", ".join(["%s"] * len(f.billto_codes))
    params: list = list(f.billto_codes)

    if f.material_codes:
        mat_placeholders = ", ".join(["%s"] * len(f.material_codes))
        material_filter = f"AND OTC_SIL_MATERIAL IN ({mat_placeholders})"
        params.extend(f.material_codes)
    else:
        material_filter = ""

    if f.date_from:
        date_from_filter = "AND OTC_SIH_INVOICE_DATE >= %s"
        params.append(f.date_from)
    else:
        date_from_filter = ""

    if f.date_to:
        date_to_filter = "AND OTC_SIH_INVOICE_DATE <= %s"
        params.append(f.date_to)
    else:
        date_to_filter = ""

    sql = AGGREGATED_SQL_TEMPLATE.format(
        qualified_table=qt,
        billto_placeholders=billto_placeholders,
        material_filter=material_filter,
        date_from_filter=date_from_filter,
        date_to_filter=date_to_filter,
    )
    return sql, params


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — EXECUTION ENGINE (read-only)
# ─────────────────────────────────────────────────────────────────────────────

# Tag every query so a DBA can find them in Snowflake's QUERY_HISTORY. The
# session-level statement timeout caps any pathological query at 60s.
_QUERY_TAG = "contract-chatbot/aggregated-retrieval"
_STMT_TIMEOUT_S = int(os.environ.get("SNOWFLAKE_AGG_TIMEOUT_S", "60"))


def execute_aggregated(sql: str, params: list) -> pd.DataFrame:
    """Run the aggregated SQL on a read-only session. Returns a DataFrame
    whose column names exactly match the SELECT aliases above."""
    conn = _get_connection()
    cur = conn.cursor()
    # Per-session safety belt — applied every call because connection pooling
    # could otherwise carry over a different role/timeout from a prior caller.
    cur.execute(f"ALTER SESSION SET QUERY_TAG = '{_QUERY_TAG}'")
    cur.execute(
        f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {_STMT_TIMEOUT_S}"
    )
    cur.execute("ALTER SESSION SET AUTOCOMMIT = TRUE")
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    cols = [d[0] for d in (cur.description or [])]
    df = pd.DataFrame(rows, columns=cols)
    df.columns = [c.lower() for c in df.columns]
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Step 4a — MARKDOWN PAYLOAD (LLM-ready)
# ─────────────────────────────────────────────────────────────────────────────

def render_markdown_payload(df: pd.DataFrame, f: Filters) -> str:
    """Format the aggregated frame as three Markdown tables (one per billing
    type) plus a header that states the query was uncapped. Designed so the
    LLM can reproduce the table or pick rows without re-interpreting raw SAP
    columns."""
    if df.empty:
        return (
            "## Aggregated billing history\n\n"
            f"**Query interpretation:** {'; '.join(f.notes) or 'no filters'}\n\n"
            "**Result:** 0 distinct material codes were ever billed to this "
            "contract's bill-to customer(s) under the given filters. This is "
            "a complete answer — there is no hidden data.\n"
        )

    lines = ["## Aggregated billing history (complete — no row cap applied)\n"]
    lines.append(
        f"**Query interpretation:** {'; '.join(f.notes) or 'no filters'}\n"
    )
    lines.append(
        f"**Coverage:** {len(df)} distinct material codes · "
        f"{int(df['line_count'].sum()):,} invoice lines aggregated · "
        f"{int(df['invoice_doc_count'].sum()):,} invoice documents · "
        f"net ${df['total_net'].sum():,.2f}\n"
    )

    for bucket in ("ONE_TIME", "PERIODIC", "RECURRING"):
        sub = df[df["billing_type"] == bucket]
        if sub.empty:
            continue
        lines.append(f"\n### {bucket} charges ({len(sub)} code(s))\n")
        lines.append(_to_markdown_table(sub))

    lines.append(
        "\n**Note to model:** the table above is the COMPLETE set of distinct "
        "material codes ever billed under the given filters. Recurring monthly "
        "lines have been collapsed into one row per code via "
        "`GROUP BY material_code, item_category`. There is no LIMIT clause "
        "and nothing has been truncated. If a code is not listed, it has not "
        "been billed to this contract."
    )
    return "\n".join(lines)


def _to_markdown_table(sub: pd.DataFrame) -> str:
    """Compact, fixed-column markdown for the most useful fields. Money is
    pre-formatted so the LLM doesn't have to reparse decimals from strings."""
    cols = [
        ("material_code",          "Code"),
        ("material_description",   "Description"),
        ("item_category",          "Cat"),
        ("first_billed",           "First billed"),
        ("last_billed",            "Last billed"),
        ("distinct_months",        "# months"),
        ("invoice_doc_count",      "# invoices"),
        ("line_count",             "# lines"),
        ("total_net",              "Total net"),
    ]
    header = "| " + " | ".join(label for _, label in cols) + " |"
    sep    = "| " + " | ".join("---" for _ in cols) + " |"
    body_rows = []
    for _, r in sub.iterrows():
        row = []
        for key, _ in cols:
            v = r.get(key)
            if key == "total_net":
                row.append(f"${float(v or 0):,.2f}")
            elif key in ("first_billed", "last_billed"):
                row.append(str(v) if v else "—")
            elif key == "material_description":
                row.append(str(v or "")[:48].replace("|", "/"))
            elif v is None:
                row.append("—")
            else:
                row.append(str(v))
        body_rows.append("| " + " | ".join(row) + " |")
    return "\n".join([header, sep] + body_rows)


# ─────────────────────────────────────────────────────────────────────────────
# Step 4b — FINAL SYNTHESIS PROMPT
# ─────────────────────────────────────────────────────────────────────────────
#
# Wraps the payload with explicit instructions about completeness. The LLM has
# a strong prior to hedge ("based on the data shown, …") which directly causes
# the bug we're fixing — users see hedged answers and assume data is missing.

SYNTHESIS_SYSTEM_PROMPT = """\
You are answering questions about historical SAP invoice billing for a
specific contract. The HUMAN_QUESTION below was used to query Snowflake via
an aggregated SQL pipeline. The DATA_PAYLOAD that follows is the COMPLETE
result — every distinct material code ever billed under the resolved filters,
with recurring monthly invoices already collapsed to one row per code.

Rules you MUST follow:

1. The data is complete. There is no row cap, no LIMIT, no truncation.
   Do NOT say "based on the data shown" or "in the data I have access to".
   If a code is not in the table, it was not billed — say so plainly.

2. Distinguish ONE_TIME vs PERIODIC vs RECURRING using the `billing_type`
   column the SQL already computed (>= 6 distinct months → RECURRING,
   2–5 → PERIODIC, 1 → ONE_TIME). Do not infer this from first/last dates
   yourself — trust the column.

3. When the user asks "what was billed", list the codes from the ONE_TIME
   section first (these are the implementation / hardware / setup fees the
   old pipeline kept missing), then PERIODIC, then RECURRING. Always include
   each code's `first_billed` and `total_net`.

4. When citing a row, use this format:
       `<CODE>` ("<description>", <billing_type>, first billed <date>,
       <line_count> line(s) across <invoice_doc_count> invoice(s),
       net $<total_net>)

5. The `Query interpretation` line at the top of the payload tells you how
   the question was parsed. If the user's intent was misread, point that out
   instead of answering the wrong question.

DATA_PAYLOAD follows below.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator — the single function the chatbot calls.
# ─────────────────────────────────────────────────────────────────────────────

def build_aggregated_context(
    question: str,
    focused_client: Optional[str] = None,
) -> str:
    """4-step pipeline. Returns the markdown payload prefixed with the
    synthesis system prompt, ready to inject into the LLM call. Never raises
    — any failure becomes an explanatory string the chatbot relays verbatim
    (mirrors snowflake_invoice.build_invoice_context's contract)."""

    if not snowflake_available():
        return _err("snowflake-connector-python not installed.")

    try:
        cfg = _load_cfg()
    except Exception as e:
        return _err(f"Could not load Snowflake config: {e}")

    try:
        info = _discover_columns(cfg)
    except Exception as e:
        return _err(f"Could not probe the SAP invoice view: {e}")
    if info["missing_required"]:
        return _err(
            "Required columns are missing from the view: "
            + ", ".join(info["missing_required"])
        )

    # Step 1
    filters = extract_filters(question, focused_client=focused_client)

    # Resolve client_name → bill-to codes (reuses the engagement bridge).
    if filters.client_name:
        try:
            matches = resolve_billto(filters.client_name, cfg)
        except Exception as e:
            return _err(f"engagement-bridge query failed: {e}")
        filters.billto_codes = [m["code"] for m in matches]
        if matches:
            filters.notes.append(
                "bill-to(s) = "
                + ", ".join(f"{m['code']} ({m['name']})" for m in matches[:3])
            )

    # Resolve contract_id → narrow material_codes via the agent map. This is
    # what makes "contract CTR-992" actually filter on the SQL side, given
    # the invoice view has no contract column.
    if filters.contract_ids and filters.client_name:
        agent_map = _agent_material_map(filters.client_name)
        wanted = {cid.upper() for cid in filters.contract_ids}
        narrowed: list[str] = []
        for rec in agent_map.get("rows", []):
            src = (rec.get("source_contract") or "").upper()
            if any(cid in src for cid in wanted):
                code = rec.get("code")
                if code and code not in narrowed:
                    narrowed.append(code)
        if narrowed:
            filters.material_codes = narrowed
            filters.notes.append(
                f"contract_id → {len(narrowed)} material code(s) via "
                "material_match_output.xlsx"
            )
        else:
            filters.notes.append(
                f"contract_id {filters.contract_ids} did not resolve to any "
                "material codes via the agent map; returning the full bill-to "
                "history instead"
            )

    if not filters.billto_codes:
        return _err(
            "Could not resolve the focused client to any SAP bill-to "
            "customer(s); no aggregated history to return."
        )

    # Step 2 + 3
    try:
        sql, params = build_aggregated_sql(filters, cfg)
        df = execute_aggregated(sql, params)
    except Exception as e:
        return _err(f"aggregated query failed: {e}")

    # Step 4
    payload = render_markdown_payload(df, filters)
    return SYNTHESIS_SYSTEM_PROMPT + "\n\n" + payload


def _err(msg: str) -> str:
    return (
        "## Aggregated billing history\n\n"
        f"**[AGGREGATED LOOKUP ERROR]** {msg}\n\n"
        "Quote this message verbatim in your answer and tell the user that "
        "aggregated billing data is currently unavailable for this question."
    )
