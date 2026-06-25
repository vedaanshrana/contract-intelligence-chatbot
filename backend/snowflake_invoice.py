"""
snowflake_invoice.py — live SAP invoice context from Snowflake.
================================================================

This module adds an INVOICE context stream to the Contract Intelligence
chatbot.  It is consulted **only** when the user's question is about
invoicing / billing / SAP (see :func:`is_invoice_query`); the rest of the
time the chatbot behaves exactly as before and never touches Snowflake.

The hard part is the **bridge** between the contract corpus (folders of PDFs
the agents extracted) and the SAP invoice rows in Snowflake.  A direct
word-for-word name match rarely works ("BLUE CROSS TEXAS FEDERAL CREDIT UNION"
folder vs. "Blue Cross Texas FCU" bill-to), so we resolve it in three layers:

  1. ENGAGEMENT bridge — fuzzy-match the focused client name to the distinct
     ``OTC_SIH_BILLTO_NAME`` values in the invoice table to find the
     ``OTC_SIH_BILLTO`` customer code(s).  Only that customer's invoices are
     then pulled.  (rapidfuzz, already a project dependency.)

  2. CONTRACT bridge — map each invoice line's ``OTC_SIL_MATERIAL`` /
     material text back to the **specific contract** the Material Code
     Matching agent linked that code to (the ``Source Contract`` column in
     ``material_match_output.xlsx`` / ``extraction_output.xlsx``).  This lets
     the answer cite the exact ``[CONTRACT]`` an invoice line belongs to,
     not just the engagement.

  3. MATERIAL reconciliation — the headline use-case.  For every material
     code we compare the **agent-dictionary** code/description against the
     **actual invoice** ``OTC_SIL_MATERIAL`` / ``OTC_SIL_MATERIAL_TEXT`` and
     classify each as:
        • MATCH         — code present and identical on both sides
        • MISMATCH      — same product but DIFFERENT codes → BOTH are surfaced
                          with their sources so the user decides
        • INVOICE-ONLY  — agent/dictionary has no code, the invoice does →
                          surface the invoice's, tagged ``[INVOICE]``
        • CONTRACT-ONLY — agent matched a code that never appears on an
                          invoice (not yet billed / different SKU)

Graceful degradation — NOTHING here raises into the chat loop:
  • ``snowflake-connector-python`` not installed → a clear notice string.
  • config TOML missing / unreadable            → a clear notice string.
  • connection / query failure                  → a clear notice string with
                                                   the underlying error.
In every failure case the chatbot keeps working on contract data alone.

Configuration — a Snowflake "connections.toml"-style file (the one whose
screenshot the user shared).  Resolution order for the file path:
  1. ``$SNOWFLAKE_CONFIG_TOML``                          (explicit override)
  2. ``<project>/snowflake_config.toml``                 (recommended spot)
  3. ``<project>/.snowflake/connections.toml``
  4. ``~/.snowflake/connections.toml``                   (Snowflake default)
The connection/section name defaults to ``$SNOWFLAKE_CONNECTION_NAME`` or,
if unset, the first ``[section]`` in the file.  Expected keys per section:
  account, user, password (or token), warehouse, database, schema, table
  (optional: role, authenticator).
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import pandas as pd

# config.OUTPUT_DIR is where the agents wrote material_match_output.xlsx etc.
try:
    from config import OUTPUT_DIR
except Exception:                                   # pragma: no cover
    OUTPUT_DIR = Path(__file__).resolve().parent / "Output"


# ─────────────────────────────────────────────────────────────────────────────
# 0.  Keyword gating — decide whether a question warrants invoice context.
# ─────────────────────────────────────────────────────────────────────────────
# Tight set so we don't drag SAP data into every unrelated question. Word-
# boundary matched so "billing" matches but "ability" (…bill…) does not.
_INVOICE_TRIGGERS = (
    r"invoice", r"invoiced", r"invoicing",
    r"bill", r"billed", r"billing", r"billable",
    r"\bsap\b",
    r"net amount", r"tax amount", r"profit center",
    r"sales office", r"sales group", r"bill[\s\-]?to",
    r"general ledger", r"\bg/?l\b",
    r"charged", r"charge amount",
    r"material code", r"material text",
)
_INVOICE_RE = re.compile("|".join(_INVOICE_TRIGGERS), re.IGNORECASE)


def is_invoice_query(text: str) -> bool:
    """True when the message mentions invoice / billing / SAP terms and the
    live invoice context should be consulted. Returns False on empty input."""
    if not text:
        return False
    return bool(_INVOICE_RE.search(text))


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Config loading (toml).
# ─────────────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent

# Columns we actually pull — the user's "most important" list. Keep this the
# single source of truth so the SELECT, the DataFrame, and the context render
# all agree.
INVOICE_COLUMNS = [
    "OTC_SIH_INVOICE_DOCUMENT",
    "OTC_SIH_INVOICE_DATE",
    "OTC_SIL_MATERIAL",
    "OTC_SIL_MATERIAL_TEXT",
    "OTC_SIL_ITEM_CATEGORY",
    "OTC_SIL_ORDER_QUANTITY",
    "OTC_SIL_NET_AMOUNT",
    "OTC_SIL_TAX_AMOUNT",
    "OTC_SIL_SALES_OFFICE",
    "OTC_SIL_SALES_OFFICE_DESCRIPTION",
    "OTC_SIL_SALES_GROUP",
    "OTC_SIH_SALES_GROUP_DESCRIPTION",
    "OTC_SIL_MAT_ASSIGN_GL_ACCOUNT",
    "OTC_SIL_MAT_ASSIGN_GL_ACCOUNTDESCRIP",
    "OTC_SIL_MAT_ASSIGN_GL_CATEGORY",
    "OTC_SIL_PROFIT_CENTER",
    "OTC_SIL_PROFIT_CENTER_NAME",
    "OTC_SIL_PRODUCT_HIERARCHY",
    "OTC_SIL_PRODUCT_HIER_NAME",
    "OTC_SIH_BILLTO",
    "OTC_SIH_BILLTO_NAME",
    "OTC_SIH_SALES_ORGANIZATION",
    "OTC_SIH_INVOICE_URL",
]

# How many invoice lines to pull per engagement (most recent first). Keeps the
# query bounded and the chat context within token limits.
MAX_INVOICE_LINES = int(os.environ.get("SNOWFLAKE_MAX_INVOICE_LINES", "400"))
# When rendering, cap lines PER INVOICE so a single fat invoice with hundreds
# of lines can't crowd out smaller invoices in the LLM context. This was
# previously a global `head(60)` which silently hid every invoice past the
# first one when that first invoice exceeded the cap.
MAX_LINES_PER_INVOICE = int(os.environ.get("SNOWFLAKE_LINES_PER_INVOICE", "15"))
MAX_INVOICES_IN_CONTEXT = int(os.environ.get("SNOWFLAKE_INVOICES_PER_CLIENT", "20"))
# How many invoice-document numbers will we try to look up directly when they
# appear in the user's question. SAP doc numbers are usually 8 digits.
MAX_DIRECT_INVOICE_LOOKUPS = int(os.environ.get("SNOWFLAKE_MAX_DIRECT_LOOKUPS", "10"))
# How many distinct material codes named in a question we look up directly
# (full-history, scoped) — see fetch_invoice_lines_by_material.
MAX_DIRECT_MATERIAL_LOOKUPS = int(os.environ.get("SNOWFLAKE_MAX_MATERIAL_LOOKUPS", "12"))
# Minimum rapidfuzz score (0-100) to accept a client↔bill-to name as the same
# engagement. 86 is conservative — tune via env if the corpus needs it.
# Minimum match score (0-100) to accept a client↔bill-to name as the same
# engagement. 90 is the SFlogic.ipynb default — corresponds to "raw substring
# match" in the tiered scoring below. Raised from the previous 86 because the
# old rapidfuzz token_set_ratio approach was producing false positives in the
# 86-95 band (e.g. matching "FIRST CHOICE CREDIT UNION" to "FIRSTENERGY CHOICE
# FEDERAL CU"). The new substring-based scoring is stricter so 90 is safe.
BILLTO_MATCH_THRESHOLD = int(os.environ.get("SNOWFLAKE_BILLTO_THRESHOLD", "90"))

# Columns that MUST exist for the integration to work at all (they appear in
# WHERE / engagement-bridge SQL, not just the SELECT list).
_REQUIRED_COLUMNS = ["OTC_SIH_BILLTO", "OTC_SIH_BILLTO_NAME"]
# Used for ORDER BY in fetch_invoice_lines. If absent, we silently drop the
# ORDER BY rather than fail the whole query.
_SORT_COLUMN = "OTC_SIH_INVOICE_DATE"

# Known column-name variants in the wild. If the canonical name (the key) is
# NOT present in the live table but one of the alternatives IS, we transparently
# SELECT the alternative AS <canonical> so downstream code is unaware of the
# substitution. The most common offender is the sales-group description, which
# carries an SIH prefix in some views and SIL in others.
_COLUMN_ALIASES: dict = {
    "OTC_SIH_SALES_GROUP_DESCRIPTION": [
        "OTC_SIL_SALES_GROUP_DESCRIPTION",
        "OTC_SIH_SALES_GRP_DESCRIPTION",
        "OTC_SIL_SALES_GRP_DESCRIPTION",
    ],
    "OTC_SIL_MAT_ASSIGN_GL_ACCOUNTDESCRIP": [
        "OTC_SIL_MAT_ASSIGN_GL_ACCOUNT_DESCRIP",
        "OTC_SIL_MAT_ASSIGN_GL_ACCT_DESCRIP",
        "OTC_SIL_MAT_ASSIGN_GL_ACCTDESCRIP",
    ],
    "OTC_SIL_SALES_OFFICE_DESCRIPTION": [
        "OTC_SIH_SALES_OFFICE_DESCRIPTION",
    ],
    "OTC_SIL_PROFIT_CENTER_NAME": [
        "OTC_SIH_PROFIT_CENTER_NAME",
    ],
    "OTC_SIL_PRODUCT_HIER_NAME": [
        "OTC_SIL_PRODUCT_HIERARCHY_NAME",
        "OTC_SIH_PRODUCT_HIER_NAME",
    ],
    # OTC_SIL_PRODUCT_HIERARCHY (the code) — common SAP variants. The
    # corresponding *_NAME column exists in the user's view, but the code
    # column appears under one of these aliases instead of the bare
    # PRODUCT_HIERARCHY identifier. Order broad → narrow so we hit the most
    # likely first.
    "OTC_SIL_PRODUCT_HIERARCHY": [
        "OTC_SIL_PRODUCT_HIER",
        "OTC_SIL_PRODUCT_HIERARCHY_CODE",
        "OTC_SIL_PRODUCT_HIER_CODE",
        "OTC_SIL_PROD_HIERARCHY",
        "OTC_SIL_PROD_HIER",
        "OTC_SIH_PRODUCT_HIERARCHY",
        "OTC_SIH_PRODUCT_HIER",
        "OTC_PRODUCT_HIERARCHY",
        "PRODUCT_HIERARCHY",
        "PROD_HIER",
    ],
}

_IDENT_RE = re.compile(r"^[A-Za-z0-9_$]+$")          # safe SQL identifier


def _candidate_config_paths() -> list[Path]:
    paths = []
    env = os.environ.get("SNOWFLAKE_CONFIG_TOML")
    if env:
        paths.append(Path(env))
    paths += [
        _REPO_ROOT / "snowflake_config.toml",
        _REPO_ROOT / ".snowflake" / "connections.toml",
        Path.home() / ".snowflake" / "connections.toml",
    ]
    return paths


def _parse_toml(path: Path) -> dict:
    """Parse a TOML file with tomllib (3.11+) → tomli → a tiny flat fallback
    for the simple ``[section]`` key="value" shape the screenshot shows."""
    data = path.read_bytes()
    try:
        import tomllib                               # Python 3.11+
        return tomllib.loads(data.decode("utf-8"))
    except Exception:
        pass
    try:
        import tomli                                 # backport
        return tomli.loads(data.decode("utf-8"))
    except Exception:
        pass
    # Minimal fallback: flat sections of  key = "value"  /  key = 123
    text = data.decode("utf-8", errors="replace")
    out: dict = {}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            section = m.group(1).strip()
            out[section] = {}
            continue
        m = re.match(r'^([A-Za-z0-9_\-]+)\s*=\s*(.+)$', line)
        if m and section is not None:
            key, val = m.group(1), m.group(2).strip()
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            out[section][key] = val
    return out


def _load_cfg() -> dict:
    """Return the resolved connection section as a plain dict, plus a
    ``_source`` key naming the file it came from. Raises RuntimeError with a
    human-readable message when nothing usable is found."""
    tried = []
    for p in _candidate_config_paths():
        tried.append(str(p))
        if not p.exists():
            continue
        try:
            parsed = _parse_toml(p)
        except Exception as e:
            raise RuntimeError(f"Could not parse Snowflake config {p}: {e}")
        if not parsed:
            continue
        # Pick the section
        name = os.environ.get("SNOWFLAKE_CONNECTION_NAME")
        if name and name in parsed and isinstance(parsed[name], dict):
            section = dict(parsed[name])
        else:
            # First dict-valued section
            section = None
            for k, v in parsed.items():
                if isinstance(v, dict):
                    section = dict(v)
                    name = k
                    break
            # Or a flat file with keys at top level
            if section is None and any(
                    k in parsed for k in ("account", "user")):
                section = dict(parsed)
                name = "(top-level)"
        if section:
            section["_source"] = f"{p}  [section: {name}]"
            return section
    raise RuntimeError(
        "No Snowflake config found. Place a connections.toml-style file at "
        "one of: " + " | ".join(tried) + ". See snowflake_config.example.toml."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Connection (lazy, cached) + small query cache.
# ─────────────────────────────────────────────────────────────────────────────
_conn_lock = threading.Lock()
_conn = None                       # cached snowflake connection
_billto_cache: dict = {}           # {source_file: (timestamp, DataFrame)}
_BILLTO_TTL = 1800                 # re-pull distinct bill-to list every 30 min


def snowflake_available() -> bool:
    """True iff the snowflake connector import succeeds. Cheap; used by the UI
    to decide whether to show an invoice status badge."""
    try:
        import snowflake.connector                   # noqa: F401
        return True
    except Exception:
        return False


def _get_connection():
    """Open (or reuse) a Snowflake connection. Caller holds no lock."""
    global _conn
    with _conn_lock:
        if _conn is not None:
            try:
                # cheap liveness check
                _conn.cursor().execute("SELECT 1").fetchone()
                return _conn
            except Exception:
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = None

        import snowflake.connector                   # lazy — optional dep
        cfg = _load_cfg()

        params = {}
        for k in ("account", "user", "warehouse", "database", "schema", "role"):
            if cfg.get(k):
                params[k] = cfg[k]
        # Auth: prefer an explicit authenticator+token (OAuth/PAT), else the
        # password field. The screenshot stores the credential under
        # `password`, which works for a Programmatic Access Token too.
        if cfg.get("authenticator"):
            params["authenticator"] = cfg["authenticator"]
        if cfg.get("token"):
            params["token"] = cfg["token"]
        elif cfg.get("password"):
            params["password"] = cfg["password"]

        _conn = snowflake.connector.connect(**params)
        return _conn


def _qualified_table(cfg: dict) -> str:
    """Build a safe ``DB.SCHEMA.TABLE`` identifier from config. Validates each
    part so a malformed config can't inject SQL."""
    db     = cfg.get("database", "")
    schema = cfg.get("schema", "")
    table  = cfg.get("table", "")
    for part, label in ((db, "database"), (schema, "schema"), (table, "table")):
        if not part or not _IDENT_RE.match(str(part)):
            raise RuntimeError(
                f"Snowflake config {label}={part!r} is missing or not a valid "
                "identifier ([A-Za-z0-9_$]).")
    return f'{db}.{schema}.{table}'


# Schema discovery — probe the live view ONCE per session and remember which of
# our wanted columns actually exist. This dodges the failure mode where a
# single wrong column name (e.g. SIH↔SIL prefix mismatch) kills the entire
# 23-column SELECT and the user sees a generic "SQL error".
_schema_lock = threading.Lock()
_schema_cache: dict = {}                  # {source_file: (timestamp, info)}
_SCHEMA_TTL = 3600                        # 1 hour — schemas rarely change


def _discover_columns(cfg: dict) -> dict:
    """Probe the configured table to find which of INVOICE_COLUMNS exist (and
    aliases for the ones that don't). Returns a dict:

        {
          "present":           [canonical columns that ARE in the table],
          "missing":           [canonical columns NOT in the table and no alias],
          "aliased":           {canonical: actual_column_name_in_table},
          "missing_required":  [REQUIRED columns the table is missing — fatal],
          "can_sort":          True iff _SORT_COLUMN exists,
          "all_existing":      {uppercase column names in the table},
        }

    Cached per config source for an hour so we don't probe on every message.
    Uses ``SELECT * ... LIMIT 0`` so it works without INFORMATION_SCHEMA grants.
    """
    src = cfg.get("_source", "?")
    now = time.time()
    with _schema_lock:
        cached = _schema_cache.get(src)
        if cached and (now - cached[0]) < _SCHEMA_TTL:
            return cached[1]

    tbl = _qualified_table(cfg)
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(f'SELECT * FROM {tbl} LIMIT 0')
    existing = {str(d[0]).upper() for d in (cur.description or [])}

    present: list[str] = []
    missing: list[str] = []
    aliased: dict = {}
    for col in INVOICE_COLUMNS:
        if col.upper() in existing:
            present.append(col)
            continue
        found = None
        for alt in _COLUMN_ALIASES.get(col, []):
            if alt.upper() in existing:
                found = alt
                break
        if found:
            present.append(col)
            aliased[col] = found
        else:
            missing.append(col)

    missing_required = [c for c in _REQUIRED_COLUMNS
                        if c.upper() not in existing]
    can_sort = _SORT_COLUMN.upper() in existing

    info = {
        "present": present,
        "missing": missing,
        "aliased": aliased,
        "missing_required": missing_required,
        "can_sort": can_sort,
        "all_existing": existing,
    }
    with _schema_lock:
        _schema_cache[src] = (now, info)
    return info


def _select_expr_for(col: str, info: dict) -> str:
    """Return the SQL fragment to SELECT this canonical column — either the
    canonical name directly, or ``<actual> AS <canonical>`` for aliased ones."""
    actual = info["aliased"].get(col)
    return f"{actual} AS {col}" if actual else col


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Name normalisation + the ENGAGEMENT bridge (client → bill-to code).
# ─────────────────────────────────────────────────────────────────────────────
_ABBREV = {
    r"\bFCU\b":  "FEDERAL CREDIT UNION",
    r"\bCU\b":   "CREDIT UNION",
    r"\bF C U\b": "FEDERAL CREDIT UNION",
    r"\bNATL\b": "NATIONAL",
    r"\bNAT'L\b": "NATIONAL",
    r"\bASSN\b": "ASSOCIATION",
    r"\bCORP\b": "CORPORATION",
    r"\bCO\b":   "COMPANY",
    r"\bINTL\b": "INTERNATIONAL",
    r"\bSVGS\b": "SAVINGS",
    r"\bSVCS\b": "SERVICES",
    r"\bBK\b":   "BANK",
}
_NOISE = re.compile(r"\b(INC|LLC|L\.L\.C|LTD|THE|A|OF|AND|&|N\.A|NA)\b")


def _normalize_name(s: str) -> str:
    """Aggressively normalise a company name for fuzzy comparison.

    Order matters: collapse punctuation to single spaces FIRST so dotted
    abbreviations like "F.C.U." become "F C U" and can then be expanded to
    "FEDERAL CREDIT UNION" by the abbreviation table. Then drop legal-suffix /
    filler noise and re-collapse spaces.
    """
    if not s:
        return ""
    out = str(s).upper()
    out = out.replace("&", " AND ")
    out = re.sub(r"[^A-Z0-9]+", " ", out)          # punctuation → space FIRST
    out = f" {out} "                                # pad so \b patterns hit edges
    for pat, rep in _ABBREV.items():
        out = re.sub(pat, f" {rep} ", out)
    out = _NOISE.sub(" ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _distinct_billto(cfg: dict) -> pd.DataFrame:
    """Return DISTINCT (OTC_SIH_BILLTO, OTC_SIH_BILLTO_NAME) — cached per
    config source with a TTL so we don't re-scan on every message."""
    src = cfg.get("_source", "?")
    now = time.time()
    cached = _billto_cache.get(src)
    if cached and (now - cached[0]) < _BILLTO_TTL:
        return cached[1]

    info = _discover_columns(cfg)
    if info["missing_required"]:
        raise RuntimeError(
            "Required columns are missing from the configured view: "
            + ", ".join(info["missing_required"])
            + ". The integration cannot resolve a client to a bill-to code "
            "without them. Verify the [section] in snowflake_config.toml "
            "points at the correct view, or update _REQUIRED_COLUMNS in "
            "snowflake_invoice.py if the view uses different identifiers.")

    tbl = _qualified_table(cfg)
    sql = (f'SELECT DISTINCT OTC_SIH_BILLTO, OTC_SIH_BILLTO_NAME '
           f'FROM {tbl} '
           f'WHERE OTC_SIH_BILLTO_NAME IS NOT NULL '
           f'LIMIT 50000')
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["OTC_SIH_BILLTO", "OTC_SIH_BILLTO_NAME"])
    df["_norm"] = df["OTC_SIH_BILLTO_NAME"].map(_normalize_name)
    _billto_cache[src] = (now, df)
    return df


# ── SFlogic-style strict normalization + variant builder ─────────────────────
# Ported from SFlogic.ipynb. The previous rapidfuzz token_set_ratio approach
# scored "FIRST CHOICE CREDIT UNION" vs "FIRSTENERGY CHOICE FEDERAL CU" in the
# high-80s because of shared tokens (CHOICE / CREDIT / UNION), passing the
# 86% threshold and silently bridging to the wrong customer.
#
# The new scoring is substring-based with three tiers:
#     1.00 — exact normalized match           (strict same string)
#     0.95 — normalized substring contains    (search-norm ⊂ stored-norm)
#     0.90 — raw case-insensitive contains    (search-raw ⊂ stored-raw)
# Normalization here is *strict*: lowercase + strip everything that isn't
# a-z or 0-9. So "FIRST CHOICE CREDIT UNION" → "firstchoicecreditunion" and
# "FIRSTENERGY CHOICE FEDERAL CU" → "firstenergychoicefederalcu". One does
# NOT contain the other → no match. That's exactly the regression we want.

_STRICT_NORM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_strict(s) -> str:
    """SFlogic-style strict normalization: lowercase + strip non-alphanumeric.
    Null-safe."""
    if s is None:
        return ""
    return _STRICT_NORM_RE.sub("", str(s).lower())


def _build_name_variants(q: str) -> list[str]:
    """Build acronym variants of a client name. Verbatim port of
    ``build_variants`` from SFlogic.ipynb.

    Returns the original + expanded (FCU → Federal Credit Union, etc.) +
    collapsed (Credit Union → CU) variants. Letting the input and stored
    value differ on acronym usage but still match is the whole point of
    this step — without it "FIRST CHOICE FCU" wouldn't match
    "FIRST CHOICE Federal Credit Union" or vice versa.

    Supports the three acronyms the original notebook handles:
        EFCU = Employees Federal Credit Union
        FCU  = Federal Credit Union
        CU   = Credit Union
    """
    q_clean = " ".join(str(q or "").split())
    if not q_clean:
        return []
    variants: set = {q_clean}

    # Expand acronyms
    expanded = q_clean
    expanded = re.sub(r"\bEFCU\b", "Employees Federal Credit Union",
                      expanded, flags=re.IGNORECASE)
    expanded = re.sub(r"\bFCU\b",  "Federal Credit Union",
                      expanded, flags=re.IGNORECASE)
    expanded = re.sub(r"\bCU\b",   "Credit Union",
                      expanded, flags=re.IGNORECASE)
    variants.add(expanded)

    # Collapse phrases back to acronyms
    collapsed = q_clean
    collapsed = re.sub(r"\bEmployees\s+Federal\s+Credit\s+Union\b", "EFCU",
                       collapsed, flags=re.IGNORECASE)
    collapsed = re.sub(r"\bFederal\s+Credit\s+Union\b",            "FCU",
                       collapsed, flags=re.IGNORECASE)
    collapsed = re.sub(r"\bCredit\s+Union\b",                       "CU",
                       collapsed, flags=re.IGNORECASE)
    variants.add(collapsed)

    # Deterministic order helps debugging / repeatability
    return sorted(v.strip() for v in variants if v and v.strip())


def resolve_billto(client_name: str, cfg: dict) -> list[dict]:
    """ENGAGEMENT BRIDGE — fuzzy-match a focused client name to the invoice
    table's bill-to customers.

    Returns ``[{code, name, score}, …]`` sorted by score desc, above the
    threshold (default 90).

    Logic: ported from SFlogic.ipynb. For each acronym variant of the input
    we score every distinct ``OTC_SIH_BILLTO_NAME`` in the invoice table
    with the highest tier that fires:

        1.00 — normalized(variant) == normalized(billto_name)
        0.95 — normalized(variant) IN normalized(billto_name)
        0.90 — variant.lower() IN billto_name.lower()
        0.00 — none of the above

    Normalization strips everything that isn't a-z or 0-9, so transient
    whitespace / punctuation / casing differences don't matter, but two
    semantically-different names with overlapping word lists DO get
    correctly rejected (the failure mode that motivated this rewrite)."""
    df = _distinct_billto(cfg)
    if df.empty:
        return []

    variants = _build_name_variants(client_name)
    if not variants:
        return []
    # Pre-normalize the variant list once; drop empties (e.g. punctuation-only).
    variant_pairs = [(v, _normalize_strict(v)) for v in variants]
    variant_pairs = [(v, vn) for v, vn in variant_pairs if vn]
    if not variant_pairs:
        return []

    scored: list[dict] = []
    for _, row in df.iterrows():
        name_raw = str(row.get("OTC_SIH_BILLTO_NAME") or "").strip()
        if not name_raw:
            continue
        name_norm = _normalize_strict(name_raw)
        if not name_norm:
            continue
        name_lower = name_raw.lower()

        # Tiered scoring — keep the best tier that any variant fires.
        score = 0.0
        for v, vnorm in variant_pairs:
            if vnorm == name_norm:
                score = 1.00
                break                              # can't beat exact
            if vnorm in name_norm:
                if score < 0.95:
                    score = 0.95
                continue
            if v.lower() in name_lower:
                if score < 0.90:
                    score = 0.90

        sc100 = int(round(score * 100))
        if sc100 >= BILLTO_MATCH_THRESHOLD:
            scored.append({
                "code":  str(row["OTC_SIH_BILLTO"]),
                "name":  name_raw,
                "score": sc100,
            })

    # Sort by score (desc), then by shorter normalized name first — when two
    # candidates score equally, the closer-length one is usually the better
    # match (e.g. "FIRST CHOICE CU" beats "FIRST CHOICE CU SOUTH BRANCH").
    scored.sort(key=lambda d: (-d["score"], len(_normalize_strict(d["name"]))))
    return scored


# ─────────────────────────────────────────────────────────────────────────────
# 3b. SCOPE resolution — client → Sold-To number(s) (bill-to fallback).
# ─────────────────────────────────────────────────────────────────────────────
# The bill-to bridge alone is too narrow for some clients: a one-off fee billed
# under a bill-to the fuzzy match missed (the "Yukon National Bank / YNB" case)
# is invisible to a bill-to-only scope. The Material Validation agent solved
# this by scoping to the client's SOLD-TO number(s) — one client = one sold-to,
# possibly several bill-tos — and also matching the sold-to NAME column. The
# chat MUST use the SAME resolution so "is code X billed to this client?" gets
# the same answer the validation agent already computed.
#
# This is the single source of truth: agents/material_validation._resolve_scope
# delegates here so the two never drift.
_SOLDTO_NUM_CANDIDATES = [
    "OTC_SIH_SOLDTO", "OTC_SIH_SOLD_TO", "OTC_SIH_SOLDTO_NUMBER",
    "OTC_SIH_SOLD_TO_NUMBER", "OTC_SIH_SOLDTO_CUSTOMER", "OTC_SIH_SOLDTO_CUST",
    "OTC_SIH_SOLD_TO_CUSTOMER", "OTC_SIL_SOLDTO", "OTC_SIL_SOLD_TO",
]
_SOLDTO_NAME_CANDIDATES = [
    "OTC_SIH_SOLDTO_NAME", "OTC_SIH_SOLD_TO_NAME", "OTC_SIH_SOLDTONAME",
    "OTC_SIH_SOLDTO_CUSTOMER_NAME", "OTC_SIH_SOLD_TO_CUSTOMER_NAME",
    "OTC_SIL_SOLDTO_NAME",
]


def _actual_col(col: str, info: dict) -> Optional[str]:
    """Actual column name for a canonical one (alias-aware), or None if absent."""
    if col in info.get("present", []):
        return info.get("aliased", {}).get(col, col)
    return None


def _first_present(cands: list, existing: set) -> Optional[str]:
    for c in cands:
        if c.upper() in existing:
            return c
    return None


def _score_name(client_name: str, stored_name: str) -> float:
    """Tiered name match (1.0 exact / 0.95 normalized-substring / 0.90 raw-
    substring) over acronym variants — identical to resolve_billto's scoring."""
    variants = _build_name_variants(client_name)
    vpairs = [(v, _normalize_strict(v)) for v in variants]
    vpairs = [(v, vn) for v, vn in vpairs if vn]
    name_norm = _normalize_strict(stored_name)
    name_lower = str(stored_name or "").lower()
    if not name_norm:
        return 0.0
    score = 0.0
    for v, vnorm in vpairs:
        if vnorm == name_norm:
            return 1.0
        if vnorm in name_norm:
            score = max(score, 0.95)
        elif v.lower() and v.lower() in name_lower:
            score = max(score, 0.90)
    return score


def resolve_scope(client_name: str, cfg: dict, info: dict, log=None) -> Optional[dict]:
    """Resolve a client to a Snowflake filter. Prefers SOLD-TO (one client =
    one sold-to, several bill-tos); folds in the sold-tos of any matched bill-to
    AND any sold-to whose NAME matches the client; falls back to bill-to scope
    when the view has no sold-to column. Returns ``{col, values, kind, label}``
    or None when nothing resolves.

    NOTE: agents/material_validation._resolve_scope delegates to this — keep the
    return shape and behaviour stable for both callers."""
    log = log or (lambda _m: None)
    existing = info.get("all_existing", set())
    tbl = _qualified_table(cfg)
    conn = _get_connection()
    billto_col = _actual_col("OTC_SIH_BILLTO", info) or "OTC_SIH_BILLTO"

    # 1) Bill-tos via the proven engagement bridge.
    try:
        billto_matches = resolve_billto(client_name, cfg)
    except Exception as e:
        log(f"  resolve_billto failed: {e}")
        billto_matches = []
    billto_codes = [m["code"] for m in billto_matches if m.get("code")]

    soldto_num = _first_present(_SOLDTO_NUM_CANDIDATES, existing)
    soldto_name = _first_present(_SOLDTO_NAME_CANDIDATES, existing)

    if not soldto_num:
        # No sold-to column → bill-to scope (the only option).
        if billto_codes:
            log(f"  Scope → BILL-TO (no sold-to column in view): "
                f"{len(billto_codes)} code(s) "
                f"[{', '.join(m['name'] for m in billto_matches[:3])}].")
            return {"col": billto_col, "values": billto_codes, "kind": "billto",
                    "label": f"{len(billto_codes)} bill-to(s)"}
        log("  Scope → NONE: no bill-to matched and no sold-to column.")
        return None

    soldtos: set = set()

    # 2a) Sold-tos of the matched bill-tos.
    if billto_codes:
        try:
            ph = ", ".join(["%s"] * len(billto_codes))
            cur = conn.cursor()
            cur.execute(
                f"SELECT DISTINCT {soldto_num} FROM {tbl} "
                f"WHERE {billto_col} IN ({ph}) AND {soldto_num} IS NOT NULL",
                tuple(str(c) for c in billto_codes))
            for (sv,) in cur.fetchall():
                if sv is not None and str(sv).strip():
                    soldtos.add(str(sv).strip())
        except Exception as e:
            log(f"  sold-to-from-bill-to lookup failed: {e}")

    # 2b) Sold-tos whose NAME matches the client (catches bill-to-name
    #     abbreviations the bill-to bridge missed — the YNB case).
    name_hits = 0
    if soldto_name:
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT DISTINCT {soldto_num}, {soldto_name} FROM {tbl} "
                f"WHERE {soldto_name} IS NOT NULL LIMIT 50000")
            for code_v, name_v in cur.fetchall():
                if code_v is None or not str(code_v).strip():
                    continue
                if _score_name(client_name, str(name_v or "")) * 100 >= BILLTO_MATCH_THRESHOLD:
                    soldtos.add(str(code_v).strip())
                    name_hits += 1
        except Exception as e:
            log(f"  sold-to name match failed: {e}")
    log(f"  Sold-to name match: {name_hits}")

    if soldtos:
        vals = sorted(soldtos)
        log(f"  Scope → SOLD-TO on {soldto_num}: {len(vals)} value(s) "
            f"{vals[:6]}{'…' if len(vals) > 6 else ''} "
            f"(from {len(billto_codes)} bill-to(s) + {name_hits} name hit(s)).")
        return {"col": soldto_num, "values": vals, "kind": "soldto",
                "label": f"sold-to {', '.join(vals[:4])}"}

    # 3) Nothing on sold-to → fall back to bill-to scope if we have any.
    if billto_codes:
        log(f"  Scope → BILL-TO fallback (sold-to unresolved): "
            f"{len(billto_codes)} code(s).")
        return {"col": billto_col, "values": billto_codes, "kind": "billto",
                "label": f"{len(billto_codes)} bill-to(s)"}
    log("  Scope → NONE: neither sold-to nor bill-to resolved.")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Pull invoice lines for the resolved bill-to code(s).
# ─────────────────────────────────────────────────────────────────────────────
def fetch_invoice_lines(billto_codes: list[str], cfg: dict,
                        limit: int = MAX_INVOICE_LINES) -> pd.DataFrame:
    """Pull the important invoice columns for the given bill-to code(s),
    most-recent first. Parameterised query (codes are data, not identifiers).

    Schema-aware: only SELECTs columns that actually exist in the live view,
    transparently aliases known variants (e.g. SIL↔SIH prefix swaps), and pads
    any genuinely-missing columns with empty strings so downstream code (the
    rendering helpers, reconciliation, the UI) never branches on whether a
    column is present. This is what stops a single wrong column name from
    killing the entire 23-column SELECT with a generic SQL compilation error.
    """
    if not billto_codes:
        return pd.DataFrame(columns=INVOICE_COLUMNS)

    info = _discover_columns(cfg)
    if info["missing_required"]:
        raise RuntimeError(
            "Required columns are missing from the configured view: "
            + ", ".join(info["missing_required"])
            + ". Cannot run the invoice query.")

    select_parts = [_select_expr_for(c, info) for c in info["present"]]
    tbl = _qualified_table(cfg)
    cols_sql = ", ".join(select_parts)
    placeholders = ", ".join(["%s"] * len(billto_codes))
    order_clause = (f"ORDER BY {_SORT_COLUMN} DESC " if info["can_sort"] else "")
    sql = (f'SELECT {cols_sql} FROM {tbl} '
           f'WHERE OTC_SIH_BILLTO IN ({placeholders}) '
           f'{order_clause}'
           f'LIMIT {int(limit)}')
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(sql, tuple(billto_codes))
    rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=info["present"])
    # Pad columns that don't exist in this view so the rest of the module sees
    # a uniform 23-column frame.
    for col in info["missing"]:
        df[col] = ""
    # Restore canonical order.
    return df[INVOICE_COLUMNS]


# ─────────────────────────────────────────────────────────────────────────────
# 4b. DIRECT invoice-number lookup
# ─────────────────────────────────────────────────────────────────────────────
# When the user's question explicitly references one or more invoice document
# numbers (e.g. "what's the bill amount for invoice 90019079?"), the regular
# bill-to-scoped fetch can miss them: it pulls the most-recent N invoices, so
# a 2022 invoice referenced by a user in 2026 falls off the window.
#
# This pair of helpers solves that:
#   1. `_extract_invoice_numbers()` mines the user's question for invoice-doc
#      candidates. To avoid grabbing dollar amounts or dates, it only matches
#      numbers preceded by an invoice-related keyword (invoice / doc / bill /
#      receipt / #).
#   2. `fetch_invoice_lines_by_document()` runs a separate query keyed on
#      OTC_SIH_INVOICE_DOCUMENT IN (...) with NO bill-to filter, so it finds
#      the requested invoices regardless of which client is currently focused
#      or how old they are.
#
# `build_invoice_context()` calls both — the direct-document data is appended
# to the context as a clearly-labelled "DIRECT INVOICE LOOKUP" block, so the
# LLM can answer questions about specific invoices even when they're outside
# the recent-window pulled by the bill-to scope.

_INVOICE_NUMBER_RE = re.compile(
    r"\b(?:invoice|invoices|inv|doc|document|bill|billed|billing|receipt|#)"
    r"\s*(?:number|num|no|#)?\s*"
    r"#?\s*(\d{6,12})\b",
    re.IGNORECASE,
)


def _extract_invoice_numbers(text: str) -> list[str]:
    """Pull candidate SAP invoice document numbers out of a chat question.

    Only matches a 6–12 digit number when it is **preceded by an invoice-
    related keyword** (invoice / doc / bill / receipt / #). This avoids the
    obvious false positives — dollar amounts like ``$1,000,000`` and dates
    like ``2026-05-28`` — that a bare ``\\d{8,}`` regex would catch.

    Returns the deduplicated list in original order, capped at
    ``MAX_DIRECT_INVOICE_LOOKUPS``."""
    if not text:
        return []
    matches = _INVOICE_NUMBER_RE.findall(text)
    seen: list = []
    for m in matches:
        if m not in seen:
            seen.append(m)
        if len(seen) >= MAX_DIRECT_INVOICE_LOOKUPS:
            break
    return seen


def fetch_invoice_lines_by_document(doc_nos: list, cfg: dict) -> pd.DataFrame:
    """Pull invoice line(s) for specific invoice document number(s) — used
    when the user asks about an invoice by its number. Does NOT filter by
    bill-to so it finds the invoice even if it's older than the recent
    window or belongs to a different client than the one currently focused.

    Returns a (possibly empty) DataFrame with the canonical INVOICE_COLUMNS
    order. Caller is responsible for telling the user when nothing came
    back."""
    if not doc_nos:
        return pd.DataFrame(columns=INVOICE_COLUMNS)

    info = _discover_columns(cfg)
    if info["missing_required"]:
        raise RuntimeError(
            "Required columns are missing from the configured view: "
            + ", ".join(info["missing_required"]))

    select_parts = [_select_expr_for(c, info) for c in info["present"]]
    tbl = _qualified_table(cfg)
    cols_sql = ", ".join(select_parts)
    placeholders = ", ".join(["%s"] * len(doc_nos))
    # Sort by document then line so the rendering groups cleanly.
    sql = (f'SELECT {cols_sql} FROM {tbl} '
           f'WHERE OTC_SIH_INVOICE_DOCUMENT IN ({placeholders}) '
           # 2000-line ceiling guards against a malformed query that
           # somehow matches a huge slice.
           f'LIMIT 2000')
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(sql, tuple(str(d) for d in doc_nos))
    rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=info["present"])
    for col in info["missing"]:
        df[col] = ""
    return df[INVOICE_COLUMNS]


# ─────────────────────────────────────────────────────────────────────────────
# 4c. DIRECT material-code lookup
# ─────────────────────────────────────────────────────────────────────────────
# The bill-to-scoped recent-window fetch (fetch_invoice_lines) can wrongly
# conclude a code "was never billed": it returns only the most-recent N lines,
# so a one-time fee billed years ago (e.g. an implementation charge) falls off
# the window. When the user asks about a SPECIFIC material code, we answer it
# the way the Material Validation agent does — a scoped query over the client's
# FULL invoice history, no date window — so the chat and the validation output
# can never disagree on whether a code was billed.

# A material code carries BOTH letters and digits (ECM0004, CNS1255,
# PROFVEL0020). That shape is what lets us tell a code apart from a bare dollar
# amount (54320), a year (2024), or an English word — none of which we want to
# treat as a code. Pure-numeric SAP material numbers are picked up via the
# agent's known-code cross-reference instead.
_MATERIAL_TOKEN_RE = re.compile(r"\b[A-Za-z0-9]{4,}\b")


def _extract_material_codes(text: str, known_codes=None) -> list[str]:
    """Pull candidate SAP material codes from a chat question.

    Two sources, unioned:
      1. Alphanumeric tokens containing BOTH a letter and a digit (ECM0004).
      2. Any agent-known code (from material_match/extraction output) mentioned
         verbatim — catches purely-numeric codes the shape rule skips.

    Returns uppercased, de-duplicated codes capped at
    ``MAX_DIRECT_MATERIAL_LOOKUPS``."""
    codes: list[str] = []
    seen: set = set()
    if text:
        for tok in _MATERIAL_TOKEN_RE.findall(text):
            tu = tok.upper()
            has_alpha = any(c.isalpha() for c in tu)
            has_digit = any(c.isdigit() for c in tu)
            if has_alpha and has_digit and tu not in seen:
                seen.add(tu)
                codes.append(tu)
    if known_codes and text:
        up = text.upper()
        for kc in known_codes:
            kcu = str(kc or "").strip().upper()
            if kcu and kcu not in seen and re.search(r"\b" + re.escape(kcu) + r"\b", up):
                seen.add(kcu)
                codes.append(kcu)
    return codes[:MAX_DIRECT_MATERIAL_LOOKUPS]


def fetch_invoice_lines_by_material(codes: list, cfg: dict,
                                    scope: Optional[dict] = None,
                                    limit: int = 2000) -> pd.DataFrame:
    """Pull invoice line(s) for specific material code(s) over the FULL history
    (no date window), optionally constrained to a client scope descriptor from
    :func:`resolve_scope`. Match is case/whitespace-insensitive on the material
    column. ``scope=None`` searches the whole table (used to tell "exists but
    for a different customer" from "doesn't exist anywhere").

    Returns a DataFrame in canonical INVOICE_COLUMNS order (possibly empty)."""
    if not codes:
        return pd.DataFrame(columns=INVOICE_COLUMNS)

    info = _discover_columns(cfg)
    if info["missing_required"]:
        raise RuntimeError(
            "Required columns are missing from the configured view: "
            + ", ".join(info["missing_required"]))
    mat_actual = _actual_col("OTC_SIL_MATERIAL", info)
    if not mat_actual:
        raise RuntimeError(
            "The invoice view has no material-code column (OTC_SIL_MATERIAL); "
            "cannot look a material code up.")

    select_parts = [_select_expr_for(c, info) for c in info["present"]]
    tbl = _qualified_table(cfg)
    cols_sql = ", ".join(select_parts)
    code_ph = ", ".join(["%s"] * len(codes))
    params: list = [str(c).strip().upper() for c in codes]
    where = f"UPPER(TRIM({mat_actual})) IN ({code_ph})"
    if scope and scope.get("values"):
        scope_ph = ", ".join(["%s"] * len(scope["values"]))
        where += f" AND {scope['col']} IN ({scope_ph})"
        params += [str(v) for v in scope["values"]]
    order_clause = (f"ORDER BY {_SORT_COLUMN} DESC " if info["can_sort"] else "")
    sql = (f'SELECT {cols_sql} FROM {tbl} '
           f'WHERE {where} '
           f'{order_clause}'
           f'LIMIT {int(limit)}')
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=info["present"])
    for col in info["missing"]:
        df[col] = ""
    return df[INVOICE_COLUMNS]


# ─────────────────────────────────────────────────────────────────────────────
# 5.  CONTRACT bridge — load the agent's material map per client.
# ─────────────────────────────────────────────────────────────────────────────
def _agent_material_map(client_name: str) -> dict:
    """Return what the Material Code Matching agent (or extraction) produced
    for a client, indexed two ways for reconciliation:

        {
          "by_code": { MATERIAL_CODE: {desc, source_contract, item, price, pages} },
          "by_norm_desc": { normalized_description: MATERIAL_CODE },
          "rows": [ {code, desc, source_contract, item, price, pages}, ... ],
          "pages_by_code_contract": {
              (MATERIAL_CODE, source_contract): [sorted_unique_pages]
          },
          "pages_by_code": { MATERIAL_CODE: [sorted_unique_pages] },   # legacy
        }

    Reads material_match_output.xlsx if present, else extraction_output.xlsx.
    Captures the **Page** (or Page Number) cell so the chat can cite the
    contract page in invoice-vs-contract comparisons — the matcher resolves
    codes via the dictionary description so a naive answer loses the page
    anchor unless we keep it on the row record explicitly.

    Pages are aggregated by the (code, source_contract) PAIR — earlier
    versions aggregated by code alone, which let a code that appeared in
    both Master-Agreement.pdf (p.5) and Amendment-XYZ.pdf (p.113) cite
    BOTH pages when only one contract was being referenced. That produced
    citations like "Master-Agreement.pdf [p.113]" where p.113 actually
    came from the amendment — invalid for the 32-page master. The
    per-contract dict prevents that cross-contract pollution."""
    out = {"by_code": {}, "by_norm_desc": {}, "rows": [],
           "pages_by_code_contract": {},
           "pages_by_code": {}}
    base = OUTPUT_DIR / client_name
    candidates = [base / "material_match_output.xlsx",
                  base / "extraction_output.xlsx"]
    src = next((p for p in candidates if p.exists()), None)
    if src is None:
        return out
    try:
        df = pd.read_excel(str(src))
    except Exception:
        return out
    if df.empty or "Material Code" not in df.columns:
        return out

    def _col(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    code_col     = "Material Code"
    desc_col     = _col("Matched Description", "Item")
    item_col     = _col("Item")
    price_col    = _col("Cleaned Price", "Price")
    contract_col = _col("Source Contract")
    # Page lives under "Page" in PORTICO/DNA extraction outputs and the
    # matching agent copies it through. Older clause-style outputs use
    # "Page Number" — accept both.
    page_col     = _col("Page", "Page Number")

    def _norm_page(v) -> int:
        """Return a positive int page number or 0 if uninterpretable —
        same logic as context_builder._format_page but as an int."""
        if v is None:
            return 0
        s = str(v).strip()
        if not s or s.lower() in ("nan", "none", "0"):
            return 0
        if s.endswith(".0"):
            s = s[:-2]
        try:
            n = int(s)
            return n if n > 0 else 0
        except (TypeError, ValueError):
            return 0

    # Aggregate pages PER (code, source_contract) so a code that appears
    # in both Master-Agreement.pdf and a long amendment doesn't end up
    # citing the amendment's page on the master.
    pages_by_code_contract: dict = {}
    pages_by_code: dict = {}   # kept for back-compat / global lookups

    for _, row in df.iterrows():
        code = str(row.get(code_col, "") or "").strip()
        if not code or code.lower() == "nan":
            continue
        desc     = str(row.get(desc_col, "") or "").strip()
        item     = str(row.get(item_col, "") or "").strip()
        price    = str(row.get(price_col, "") or "").strip()
        contract = str(row.get(contract_col, "") or "").strip()
        page_n   = _norm_page(row.get(page_col)) if page_col else 0
        rec = {"code": code, "desc": desc, "source_contract": contract,
               "item": item, "price": price, "page": page_n}
        out["rows"].append(rec)
        out["by_code"].setdefault(code, rec)
        nd = _normalize_name(desc or item)
        if nd:
            out["by_norm_desc"].setdefault(nd, code)
        if page_n > 0 and contract:
            pages_by_code_contract.setdefault(
                (code, contract), set()).add(page_n)
            pages_by_code.setdefault(code, set()).add(page_n)

    out["pages_by_code_contract"] = {
        k: sorted(v) for k, v in pages_by_code_contract.items()
    }
    out["pages_by_code"] = {
        c: sorted(ps) for c, ps in pages_by_code.items()
    }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 6.  MATERIAL reconciliation — the headline output.
# ─────────────────────────────────────────────────────────────────────────────
def reconcile_materials(agent_map: dict, invoice_df: pd.DataFrame) -> dict:
    """Compare agent-dictionary material codes against the actual invoice
    material codes. Returns buckets:

        {
          "match":        [ {code, desc, source_contract, invoice_text, invoice_docs} ],
          "mismatch":     [ {agent_code, agent_desc, source_contract,
                             invoice_code, invoice_text, invoice_docs} ],
          "invoice_only": [ {invoice_code, invoice_text, invoice_docs, product_hier} ],
          "contract_only":[ {code, desc, source_contract, item, price} ],
        }

    MISMATCH definition: the SAME product (matched by normalized description /
    product-hier name) carries DIFFERENT codes on the two sides. We surface
    BOTH codes and their sources and let the user judge — never silently pick
    one (the user's explicit requirement)."""
    res = {"match": [], "mismatch": [], "invoice_only": [], "contract_only": []}
    if invoice_df is None:
        invoice_df = pd.DataFrame(columns=INVOICE_COLUMNS)

    # Index invoice lines by material code and by normalized text.
    inv_by_code: dict = {}
    inv_by_norm: dict = {}
    for _, r in invoice_df.iterrows():
        icode = str(r.get("OTC_SIL_MATERIAL", "") or "").strip()
        itext = str(r.get("OTC_SIL_MATERIAL_TEXT", "") or "").strip()
        phier = str(r.get("OTC_SIL_PRODUCT_HIER_NAME", "") or "").strip()
        idoc = str(r.get("OTC_SIH_INVOICE_DOCUMENT", "") or "").strip()
        if icode and icode.lower() != "nan":
            b = inv_by_code.setdefault(icode, {"text": itext, "phier": phier,
                                               "docs": set()})
            if idoc:
                b["docs"].add(idoc)
        nd = _normalize_name(itext or phier)
        if nd:
            b = inv_by_norm.setdefault(nd, {"code": icode, "text": itext,
                                            "phier": phier, "docs": set()})
            if idoc:
                b["docs"].add(idoc)

    matched_invoice_codes: set = set()
    # Page set per (material code, source contract) pair — pulled from
    # _agent_material_map. Keyed by BOTH code AND contract because the
    # same code can appear in multiple contracts (master + amendments)
    # at different pages; mixing them produces invalid citations like
    # "Master-Agreement.pdf [p.113]" when p.113 actually came from an
    # amendment of the master.
    pages_by_code_contract: dict = (
        agent_map.get("pages_by_code_contract", {}) or {}
    )

    def _contract_pages(code: str, contract: str) -> list:
        """Sorted distinct pages of `contract` where this code appears.
        Returns [] when the (code, contract) pair has no recorded pages."""
        if not contract:
            return []
        return pages_by_code_contract.get((code, contract), [])

    # Walk the agent's codes first.
    for rec in agent_map.get("rows", []):
        acode = rec["code"]
        adesc = rec["desc"] or rec["item"]
        nd = _normalize_name(adesc)

        if acode in inv_by_code:
            # Same code present on an invoice — MATCH.
            matched_invoice_codes.add(acode)
            ib = inv_by_code[acode]
            res["match"].append({
                "code": acode, "desc": adesc,
                "source_contract": rec["source_contract"],
                "contract_pages": _contract_pages(acode, rec["source_contract"]),
                "invoice_text": ib["text"],
                "invoice_docs": sorted(ib["docs"]),
            })
        elif nd and nd in inv_by_norm and inv_by_norm[nd]["code"]:
            # Same product (by description), DIFFERENT code — MISMATCH.
            ib = inv_by_norm[nd]
            matched_invoice_codes.add(ib["code"])
            res["mismatch"].append({
                "agent_code": acode, "agent_desc": adesc,
                "source_contract": rec["source_contract"],
                "contract_pages": _contract_pages(acode, rec["source_contract"]),
                "invoice_code": ib["code"], "invoice_text": ib["text"],
                "invoice_docs": sorted(ib["docs"]),
            })
        else:
            # Agent has it; no invoice line found — CONTRACT-ONLY.
            res["contract_only"].append({
                "code": acode, "desc": adesc,
                "source_contract": rec["source_contract"],
                "contract_pages": _contract_pages(acode, rec["source_contract"]),
                "item": rec["item"], "price": rec["price"],
            })

    # Any invoice code we never reconciled = INVOICE-ONLY (agent/dictionary
    # missed it). This is the "use the invoice's, tag [INVOICE]" case.
    agent_codes = set(agent_map.get("by_code", {}).keys())
    for icode, ib in inv_by_code.items():
        if icode in matched_invoice_codes or icode in agent_codes:
            continue
        res["invoice_only"].append({
            "invoice_code": icode, "invoice_text": ib["text"],
            "product_hier": ib["phier"], "invoice_docs": sorted(ib["docs"]),
        })
    return res


# ─────────────────────────────────────────────────────────────────────────────
# 7.  Per-session link registry (so the UI can render [INVOICE] links/viewer).
# ─────────────────────────────────────────────────────────────────────────────
_last_links_lock = threading.Lock()
_last_invoice_links: dict = {}     # {invoice_document: url}


def _register_invoice_links(invoice_df: pd.DataFrame) -> None:
    """Capture {invoice_document: url} for every invoice we pulled this turn.

    Important: register EVERY invoice doc, even ones with no URL — store empty
    string for the URL in that case. This lets ``_find_cited_invoices`` in
    chatbot.py detect citations of URL-less invoices too (the UI then renders a
    "no URL available" note instead of skipping the citation altogether,
    which was a bug — invoices that lacked a URL became completely invisible
    to the link-rendering layer)."""
    with _last_links_lock:
        _last_invoice_links.clear()
        if invoice_df is None or invoice_df.empty:
            return
        for _, r in invoice_df.iterrows():
            doc = str(r.get("OTC_SIH_INVOICE_DOCUMENT", "") or "").strip()
            url = str(r.get("OTC_SIH_INVOICE_URL", "") or "").strip()
            if not doc:
                continue
            if url.lower() == "nan":
                url = ""
            _last_invoice_links.setdefault(doc, url)


def get_last_invoice_links() -> dict:
    """Return {invoice_document: url} captured on the most recent
    build_invoice_context() call (for the UI to render clickable links)."""
    with _last_links_lock:
        return dict(_last_invoice_links)


# ─────────────────────────────────────────────────────────────────────────────
# 8.  Public entry point — build the invoice context block for the prompt.
# ─────────────────────────────────────────────────────────────────────────────
# Short, identifiable banner for every error path. We instruct the LLM to QUOTE
# this verbatim so the user sees the real cause instead of a hallucinated
# summary ("SQL error", "data pull issue", etc).
_ERR_BANNER = "[INVOICE LOOKUP ERROR]"
_ERR_QUOTE_INSTRUCTION = (
    " When answering, do NOT paraphrase this error as a generic 'SQL error' "
    "or 'data pull issue'. Quote the error message verbatim so the user can "
    "act on it, then say invoice data is currently unavailable for this "
    "question (answer the rest from contract data if you can)."
)


def build_invoice_context(client_names: list[str], question: str = "") -> str:
    """Return a chat-ready INVOICE context block for the focused client(s).

    Safe to call unconditionally — but the chatbot only calls it when
    :func:`is_invoice_query` is true, to avoid a Snowflake round-trip on every
    message. Never raises: any failure becomes a notice string the LLM relays
    verbatim to the user."""
    # 1) Connector present?
    if not snowflake_available():
        return ("=== SAP INVOICE DATA ===\n"
                f"{_ERR_BANNER} snowflake-connector-python is not installed in "
                "this environment. Install it on the VDI with "
                "`pip install snowflake-connector-python`."
                + _ERR_QUOTE_INSTRUCTION)

    # 2) Config present?
    try:
        cfg = _load_cfg()
    except Exception as e:
        return ("=== SAP INVOICE DATA ===\n"
                f"{_ERR_BANNER} Could not load Snowflake config: {e}\n"
                "Copy snowflake_config.example.toml → snowflake_config.toml and "
                "paste the real PAT into the password field."
                + _ERR_QUOTE_INSTRUCTION)

    # 3) Probe the live view schema ONCE (cached) — catches host/auth/grant
    # problems AND the column-name mismatch case in a single place.
    try:
        schema_info = _discover_columns(cfg)
    except Exception as e:
        return ("=== SAP INVOICE DATA ===\n"
                f"{_ERR_BANNER} Could not query the SAP invoice view "
                f"{cfg.get('database')}.{cfg.get('schema')}.{cfg.get('table')}.\n"
                f"Underlying error: {e}\n"
                "Common causes: (1) you're not on the Fiserv VDI PrivateLink "
                f"network so the host {cfg.get('account')} doesn't resolve; "
                "(2) the credential in snowflake_config.toml is wrong, expired, "
                "or in the wrong field (PATs go in `password`); (3) the "
                "configured database/schema/table doesn't exist or your role "
                "lacks USAGE/SELECT privilege on it."
                + _ERR_QUOTE_INSTRUCTION)

    if schema_info["missing_required"]:
        return ("=== SAP INVOICE DATA ===\n"
                f"{_ERR_BANNER} The configured view is missing columns the "
                "integration absolutely needs: "
                + ", ".join(schema_info["missing_required"])
                + ".\nVerify snowflake_config.toml points at the SAP "
                "consolidated invoice view, or update _REQUIRED_COLUMNS in "
                "snowflake_invoice.py if the columns were renamed in the "
                "source system."
                + _ERR_QUOTE_INSTRUCTION)

    blocks: list[str] = []
    blocks.append("=== SAP INVOICE DATA (live from Snowflake) ===")
    blocks.append(
        "This is LIVE billing data from SAP. When citing it, tag the source "
        "[INVOICE] and include the invoice document number and its URL. Tag "
        "contract-derived facts [CONTRACT]. For material codes, follow the "
        "MATERIAL CODE RECONCILIATION rules below exactly."
    )

    # Schema notes — surfaced so the user knows when a column is alias-mapped
    # (still works) vs. genuinely absent (rendered as empty).
    if schema_info["aliased"] or schema_info["missing"]:
        notes = []
        if schema_info["aliased"]:
            notes.append("alias-mapped (canonical ← actual): " + ", ".join(
                f"{k} ← {v}" for k, v in schema_info["aliased"].items()))
        if schema_info["missing"]:
            notes.append("returned as empty (column not present in view): "
                         + ", ".join(schema_info["missing"]))
        blocks.append("(schema notes — " + " | ".join(notes) + ")")

    all_invoice_frames = []
    any_resolved = False

    for client in client_names:
        # 4) Engagement bridge.
        try:
            matches = resolve_billto(client, cfg)
        except Exception as e:
            blocks.append(
                f"\n— {client}: {_ERR_BANNER} engagement-bridge query failed: "
                f"{e}")
            continue

        if not matches:
            blocks.append(
                f"\n— {client}: no SAP bill-to customer fuzzy-matched this "
                f"client name (threshold {BILLTO_MATCH_THRESHOLD}). No invoice "
                "data to show. Tell the user no SAP invoices could be linked "
                "to this client.")
            continue

        any_resolved = True
        codes = [m["code"] for m in matches]
        bridge_note = ", ".join(
            f'{m["name"]} (bill-to {m["code"]}, match {m["score"]}%)'
            for m in matches[:4])
        blocks.append(f"\n— {client}\n  Bridged to SAP customer(s): {bridge_note}")

        # 5) Pull invoice lines.
        try:
            inv = fetch_invoice_lines(codes, cfg)
        except Exception as e:
            blocks.append(
                f"  {_ERR_BANNER} invoice-line query failed: {e}"
                + _ERR_QUOTE_INSTRUCTION)
            continue
        if inv.empty:
            blocks.append("  (bill-to resolved but no invoice lines returned "
                          "for this customer — there may simply be no recent "
                          "invoices.)")
            continue
        all_invoice_frames.append(inv)

        n_docs = inv["OTC_SIH_INVOICE_DOCUMENT"].nunique()
        blocks.append(f"  {len(inv)} invoice line(s) across {n_docs} invoice "
                      f"document(s) (most recent first, capped at "
                      f"{MAX_INVOICE_LINES}).")

        # 6) CONTRACT bridge + 7) reconciliation.
        agent_map = _agent_material_map(client)
        rec = reconcile_materials(agent_map, inv)

        blocks.append(_render_reconciliation(rec))
        blocks.append(_render_invoice_lines(inv))

    # ── 8) DIRECT invoice-number lookup ────────────────────────────────────
    # If the user's question explicitly named one or more invoice document
    # numbers (e.g. "what's the bill amount for invoice 90019079?"), the
    # bill-to-scoped fetch above can miss them — it only returns the most
    # recent N invoices, so a 2022 invoice falls off the window. We pull
    # those documents by their numbers directly, regardless of bill-to,
    # and append them as a separate clearly-labelled block.
    direct_docs = _extract_invoice_numbers(question)
    if direct_docs:
        blocks.append(
            f"\n— DIRECT INVOICE LOOKUP — the user's question referenced "
            f"invoice number(s): {', '.join(direct_docs)}. Pulling these "
            "directly from SAP (no bill-to filter, no date window)."
        )
        try:
            direct_df = fetch_invoice_lines_by_document(direct_docs, cfg)
        except Exception as e:
            blocks.append(
                f"  {_ERR_BANNER} direct-document query failed: {e}"
                + _ERR_QUOTE_INSTRUCTION)
        else:
            found_docs = set(
                direct_df["OTC_SIH_INVOICE_DOCUMENT"].astype(str).str.strip()
            ) if not direct_df.empty else set()
            not_found = [d for d in direct_docs if d not in found_docs]

            if not_found:
                blocks.append(
                    "  ⚠ NOT FOUND in SAP: "
                    + ", ".join(not_found)
                    + " — tell the user these invoice numbers do not exist "
                    "in the SAP invoice view (they may be miswritten, from a "
                    "different system, or never created)."
                )

            if not direct_df.empty:
                blocks.append(
                    f"  ✓ Found {len(direct_df)} line(s) across "
                    f"{len(found_docs)} invoice document(s):"
                )
                # Annotate cross-client invoices: if a directly-looked-up
                # invoice belongs to a bill-to that isn't one of the focused
                # client's resolved bill-tos, the LLM should call that out.
                if client_names:
                    # Collect every resolved bill-to code across focused clients
                    focused_codes: set = set()
                    for cn in client_names:
                        try:
                            for m in resolve_billto(cn, cfg):
                                focused_codes.add(str(m["code"]))
                        except Exception:
                            pass
                    if focused_codes:
                        direct_billtos = set(
                            direct_df["OTC_SIH_BILLTO"].astype(str).str.strip()
                        )
                        cross_client = direct_billtos - focused_codes
                        if cross_client:
                            blocks.append(
                                "  NOTE: one or more of these invoices belong "
                                f"to bill-to code(s) {sorted(cross_client)} "
                                "that are NOT the currently-focused client. "
                                "When answering, explicitly tell the user the "
                                "invoice belongs to a different SAP customer."
                            )

                blocks.append(_render_invoice_lines(direct_df))
                all_invoice_frames.append(direct_df)
                any_resolved = True

    # ── 8b) DIRECT MATERIAL CODE lookup ────────────────────────────────────
    # When the user names a specific material code, the recent-window fetch
    # above can miss a one-time / old charge and wrongly report "never billed".
    # Resolve each focused client's full scope (sold-to, exactly like the
    # Material Validation agent) and search its ENTIRE invoice history for the
    # code(s), so the chat can never contradict the validation output.
    known_codes: set = set()
    for cn in client_names:
        try:
            known_codes |= set(_agent_material_map(cn).get("by_code", {}).keys())
        except Exception:
            pass
    direct_codes = _extract_material_codes(question, known_codes)
    if direct_codes:
        blocks.append(
            "\n— DIRECT MATERIAL CODE LOOKUP — the user's question referenced "
            f"material code(s): {', '.join(direct_codes)}. Searching each "
            "focused client's FULL SAP invoice history (scoped to the client's "
            "sold-to / bill-to, NO date window). THIS is the authoritative "
            "billed-or-not check: if a code shows ✓ here it WAS billed to the "
            "client even when it is absent from the recent INVOICE LINE DETAIL "
            "or RECONCILIATION above — never tell the user a ✓ code was not "
            "billed.")
        found_in_scope: set = set()
        for client in client_names:
            try:
                scope = resolve_scope(client, cfg, schema_info)
            except Exception as e:
                blocks.append(f"  • {client}: {_ERR_BANNER} scope resolve failed: {e}")
                continue
            if not scope:
                blocks.append(
                    f"  • {client}: no SAP sold-to/bill-to scope resolved — "
                    "cannot confirm billing for these codes.")
                continue
            try:
                mdf = fetch_invoice_lines_by_material(direct_codes, cfg, scope=scope)
            except Exception as e:
                blocks.append(
                    f"  • {client}: {_ERR_BANNER} material lookup failed: {e}"
                    + _ERR_QUOTE_INSTRUCTION)
                continue
            if not mdf.empty:
                all_invoice_frames.append(mdf)
                any_resolved = True
            present = set(
                mdf["OTC_SIL_MATERIAL"].astype(str).str.strip().str.upper()
            ) if not mdf.empty else set()
            blocks.append(f"  • {client} (scope: {scope.get('label', '?')}):")
            for code in direct_codes:
                if code in present:
                    found_in_scope.add(code)
                    blocks.append("      " + _summarize_material_hits(code, mdf))
                else:
                    blocks.append(
                        f"      ✗ {code}: NOT billed to this client "
                        "(full invoice history searched).")
        # Disambiguate codes absent from EVERY focused client: do they exist at
        # all (billed to another customer) or nowhere in SAP?
        missing = [c for c in direct_codes if c not in found_in_scope]
        if missing:
            try:
                gdf = fetch_invoice_lines_by_material(missing, cfg, scope=None, limit=400)
            except Exception:
                gdf = pd.DataFrame(columns=INVOICE_COLUMNS)
            gpresent = set(
                gdf["OTC_SIL_MATERIAL"].astype(str).str.strip().str.upper()
            ) if not gdf.empty else set()
            for code in missing:
                if code in gpresent:
                    sub = gdf[gdf["OTC_SIL_MATERIAL"].astype(str).str.strip().str.upper() == code]
                    others = [n for n in dict.fromkeys(
                        str(x).strip() for x in sub["OTC_SIH_BILLTO_NAME"])
                        if n and n.lower() != "nan"]
                    blocks.append(
                        f"  • {code}: EXISTS in SAP but billed to OTHER "
                        f"customer(s) — e.g. {', '.join(others[:3]) or '?'} — "
                        "NOT the focused client(s). Tell the user it is a real "
                        "SAP code but not on their invoices.")
                else:
                    blocks.append(
                        f"  • {code}: does NOT appear anywhere in the SAP "
                        "invoice view (no invoice line has ever used this code).")

    # Register links for the UI from everything we pulled this turn.
    if all_invoice_frames:
        merged = pd.concat(all_invoice_frames, ignore_index=True)
        _register_invoice_links(merged)
    else:
        _register_invoice_links(pd.DataFrame(columns=INVOICE_COLUMNS))

    if not any_resolved:
        blocks.append(
            "\n(No SAP invoices linked to the focused client(s). Answer from "
            "contract data only and note that invoice data could not be "
            "matched.)")

    return "\n".join(blocks)


# ── Rendering helpers ─────────────────────────────────────────────────────────
def _fmt_money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        s = str(v or "").strip()
        return s if s and s.lower() != "nan" else "?"


def _safe_float(v) -> float:
    """Parse a value as a float for summing. Returns 0.0 on any failure so
    a single bad row can't break a SUM(net) call.

    Handles:
      - None / pandas NaN / empty string  → 0.0
      - Plain numerics (int, float)       → the number
      - "$1,234.56" / "1,234.56"          → 1234.56
      - "(1,234.56)" (accounting neg.)    → -1234.56
      - "12M" (asset-size sentinels)      → 0.0 (these are not real amounts)
    """
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return 0.0 if v != v else float(v)        # NaN safety
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return 0.0
    s = s.replace(",", "").replace("$", "").strip()
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    # Reject "12M" / "500K" — those are asset-size sentinels, not fees.
    if re.match(r"^-?\d+(\.\d+)?\s*[MBKmbk]$", s):
        return 0.0
    try:
        out = float(s)
    except (TypeError, ValueError):
        return 0.0
    return -out if neg else out


def _invoice_totals(grp: "pd.DataFrame") -> tuple[float, float]:
    """Return (net_total, tax_total) summed across every row in `grp`.
    Both column names default to empty if absent — defensive against the
    schema-discovery aliasing path having dropped them."""
    net = sum(_safe_float(v) for v in grp.get("OTC_SIL_NET_AMOUNT", []))
    tax = sum(_safe_float(v) for v in grp.get("OTC_SIL_TAX_AMOUNT", []))
    return float(net), float(tax)


def _summarize_material_codes(grp: "pd.DataFrame", max_codes: int = 200) -> str:
    """Compact one-line summary listing EVERY distinct material code used in
    this invoice's line items, with ``(×N)`` counts for repeats. Lines like

        CNS1255 "CREDIT AUTHORIZATION - 48701" (×80), PROFVEL0020 "PRO VEL" (×3), ...

    Emitted after the per-line full-detail block so material-code questions
    are always answerable from the prompt even when the full-detail head
    cap (`MAX_LINES_PER_INVOICE`) hides some lines. Material codes are the
    PRIMARY value of the Snowflake integration — we never let them get
    silently truncated.

    Returns the joined string, or an empty-state notice if no codes."""
    from collections import OrderedDict
    bucket: "OrderedDict[str, dict]" = OrderedDict()
    for _, r in grp.iterrows():
        code = str(r.get("OTC_SIL_MATERIAL", "") or "").strip()
        if not code or code.lower() == "nan":
            continue
        text = str(r.get("OTC_SIL_MATERIAL_TEXT", "") or "").strip()
        if text.lower() == "nan":
            text = ""
        if code not in bucket:
            bucket[code] = {"text": text[:40], "count": 1}
        else:
            bucket[code]["count"] += 1
            # First non-empty description wins (consistent across the group)
            if text and not bucket[code]["text"]:
                bucket[code]["text"] = text[:40]
    if not bucket:
        return "(no material codes on any line of this invoice)"

    items = list(bucket.items())
    truncated = False
    if len(items) > max_codes:
        items = items[:max_codes]
        truncated = True

    parts = []
    for code, info in items:
        suffix = f" (×{info['count']})" if info["count"] > 1 else ""
        if info["text"]:
            parts.append(f'{code} "{info["text"]}"{suffix}')
        else:
            parts.append(f'{code}{suffix}')
    out = ", ".join(parts)
    if truncated:
        out += f"  …(+{len(bucket) - max_codes} more distinct codes — rare)"
    return out


def _fmt_contract_pages(pages) -> str:
    """Format a list of contract page ints as ``[p.1, p.5, p.12]`` for
    inline use in a [CONTRACT] citation. Returns ``""`` when empty so
    callers can drop it cleanly."""
    if not pages:
        return ""
    return " [" + ", ".join(f"p.{n}" for n in pages) + "]"


def _summarize_material_hits(code: str, df: pd.DataFrame) -> str:
    """One authoritative line for a directly-looked-up material code: whether it
    was billed, how many lines / invoices, total net, and up to 3 example
    invoices (doc, date, net). `df` is the result of
    fetch_invoice_lines_by_material — already scoped/filtered to the code(s)."""
    if df is None or df.empty:
        return f"✗ {code}: not billed."
    sub = df[df["OTC_SIL_MATERIAL"].astype(str).str.strip().str.upper() == code]
    if sub.empty:
        return f"✗ {code}: not billed."
    docs = [d for d in dict.fromkeys(
        str(x).strip() for x in sub["OTC_SIH_INVOICE_DOCUMENT"]) if d and d.lower() != "nan"]
    sample_text = ""
    for t in sub["OTC_SIL_MATERIAL_TEXT"]:
        ts = str(t or "").strip()
        if ts and ts.lower() != "nan":
            sample_text = ts[:50]
            break
    net_total = sum(_safe_float(v) for v in sub.get("OTC_SIL_NET_AMOUNT", []))
    examples = []
    for doc, g in list(sub.groupby("OTC_SIH_INVOICE_DOCUMENT", sort=False))[:3]:
        first = g.iloc[0]
        date = str(first.get("OTC_SIH_INVOICE_DATE", "") or "").strip()
        dnet = sum(_safe_float(v) for v in g.get("OTC_SIL_NET_AMOUNT", []))
        examples.append(f"{doc} ({date}, net ${dnet:,.2f})")
    more = " …" if len(docs) > 3 else ""
    desc = f' "{sample_text}"' if sample_text else ""
    return (f"✓ {code}{desc} — IS billed: {len(sub)} line(s) across "
            f"{len(docs)} invoice(s), total net ${net_total:,.2f}. "
            f"e.g. {'; '.join(examples)}{more}")


def _render_reconciliation(rec: dict) -> str:
    """Render the MATERIAL CODE RECONCILIATION block — the highest-value part.
    Spells out, per the user's rules, how the LLM should present each case.

    Each [CONTRACT] line now includes the contract page bracket
    ``[p.1, p.5]`` when the material match data carried a Page (it does
    — the matcher passes Page through from extraction_output.xlsx). The
    matching pipeline goes via dictionary description so a naive answer
    loses the page anchor; surfacing it here keeps invoice-vs-contract
    answers fully cited."""
    lines = ["\n  === MATERIAL CODE RECONCILIATION (agent dictionary vs SAP invoice) ==="]
    lines.append(
        "  RULES: • MISMATCH → present BOTH codes with their sources, let the "
        "user decide which is correct. • INVOICE-ONLY → the agent/dictionary "
        "missed it; use the invoice's code and tag [INVOICE]. • CONTRACT-ONLY "
        "→ agent matched it but it isn't on any invoice (note it isn't billed "
        "yet). • MATCH → both agree, state the code with confidence.\n"
        "  PAGE CITATIONS: every [CONTRACT] entry below ends with a "
        "[p.N, p.M] bracket listing the contract pages where that material "
        "code was extracted. Copy that bracket VERBATIM into the Sources "
        "block of your answer alongside the contract filename — it's the "
        "back-tracked page anchor for the material code (the matcher went "
        "via dictionary description so the page would otherwise be lost)."
    )

    mm = rec.get("mismatch", [])
    io = rec.get("invoice_only", [])
    co = rec.get("contract_only", [])
    ma = rec.get("match", [])

    if mm:
        lines.append(f"\n  ⚠ MISMATCH ({len(mm)}) — DIFFERENT codes for the same product:")
        for r in mm[:60]:
            docs   = ", ".join(r["invoice_docs"][:3]) or "?"
            pp     = _fmt_contract_pages(r.get("contract_pages") or [])
            lines.append(
                f"    • \"{r['agent_desc'][:60]}\"\n"
                f"        [CONTRACT] dictionary code = {r['agent_code']} "
                f"(from {r['source_contract'] or '?'}{pp})\n"
                f"        [INVOICE]  SAP code        = {r['invoice_code']} "
                f"(\"{r['invoice_text'][:50]}\"; invoice {docs})")
    if io:
        lines.append(f"\n  ➕ INVOICE-ONLY ({len(io)}) — on SAP invoice, NOT matched by the agent:")
        for r in io[:60]:
            docs = ", ".join(r["invoice_docs"][:3]) or "?"
            lines.append(
                f"    • [INVOICE] code {r['invoice_code']} — "
                f"\"{r['invoice_text'][:60]}\""
                + (f" (product hier: {r['product_hier'][:40]})" if r['product_hier'] else "")
                + f"; invoice {docs}")
    if co:
        lines.append(f"\n  ➖ CONTRACT-ONLY ({len(co)}) — agent matched a code with no invoice line:")
        for r in co[:40]:
            pp = _fmt_contract_pages(r.get("contract_pages") or [])
            lines.append(
                f"    • [CONTRACT] code {r['code']} — \"{r['desc'][:60]}\" "
                f"(from {r['source_contract'] or '?'}{pp}; not yet billed)")
    if ma:
        lines.append(f"\n  ✓ MATCH ({len(ma)}) — code agrees on both sides:")
        for r in ma[:40]:
            docs = ", ".join(r["invoice_docs"][:3]) or "?"
            pp   = _fmt_contract_pages(r.get("contract_pages") or [])
            lines.append(
                f"    • code {r['code']} — \"{r['desc'][:60]}\" "
                f"([CONTRACT] {r['source_contract'] or '?'}{pp} / "
                f"[INVOICE] {docs})")
    if not (mm or io or co or ma):
        lines.append("  (No material codes to reconcile for this client.)")
    return "\n".join(lines)


def _render_invoice_lines(inv: pd.DataFrame,
                           max_per_invoice: int = None,
                           max_invoices: int = None) -> str:
    """Render the raw invoice line detail so the LLM can answer quantity /
    amount / GL / profit-center / sales-office questions and cite the invoice
    document + URL.

    Two caps now, both PER-INVOICE rather than the previous global head(N):
      * ``max_per_invoice`` — how many lines from each invoice (default
        MAX_LINES_PER_INVOICE).
      * ``max_invoices`` — how many distinct invoices to show (default
        MAX_INVOICES_IN_CONTEXT).

    Why this changed: the old global cap silently hid every invoice past the
    first one whenever that first invoice had more lines than the cap. That
    broke questions like "show me the latest 5 invoices for this client" —
    we'd return 5 line items from the most recent invoice and nothing from
    the other four.

    URL handling: the URL is emitted on its OWN line with an explicit
    "cite-this-URL-for" prefix so the model can't skim past it. When SAP has
    no URL for an invoice, an explicit "(URL not provided in SAP …)" line is
    emitted so the model says exactly that in Sources, instead of inventing
    placeholders like "(see SAP INVOICE DATA above)".
    """
    if max_per_invoice is None:
        max_per_invoice = MAX_LINES_PER_INVOICE
    if max_invoices is None:
        max_invoices = MAX_INVOICES_IN_CONTEXT

    lines = ["\n  === INVOICE LINE DETAIL ==="]
    lines.append(
        "  When citing one of these invoices in Sources, use this EXACT form:\n"
        "    [INVOICE] <doc> — <URL from the 'URL:' line below>\n"
        "  If the 'URL:' line says '(not provided in SAP)', cite it as:\n"
        "    [INVOICE] <doc> — (URL not provided in SAP)\n"
        "  Never invent a placeholder like '(see SAP INVOICE DATA above)'."
    )
    lines.append(
        "  Each invoice block below includes a PRE-COMPUTED 'Totals:' line "
        "with net / tax / gross summed across ALL line items of that "
        "invoice (including lines we didn't render due to space). When the "
        "user asks for an invoice total, READ THAT TOTALS LINE — do NOT "
        "re-sum the visible lines, those totals already account for any "
        "hidden lines."
    )
    lines.append(
        "  Each invoice block also includes an 'All material codes on this "
        "invoice:' line that lists EVERY distinct material code used on "
        "that invoice (with ×N counts for repeats), even if the per-line "
        "detail above is capped. When the user asks 'what material codes "
        "were billed on this invoice', READ THAT LINE — it is the "
        "authoritative, complete code list. The per-line block above is "
        "only a sample for net/tax/GL/profit-center context."
    )
    if inv.empty:
        lines.append("  (no invoice lines to render)")
        return "\n".join(lines)

    # Group by document FIRST, preserving the most-recent-first order from the
    # source query. groupby(sort=False) respects the existing row order, so the
    # first group is the most-recent invoice document.
    groups = list(inv.groupby("OTC_SIH_INVOICE_DOCUMENT", sort=False))
    shown_groups = groups[:max_invoices]
    skipped_invoices = len(groups) - len(shown_groups)

    # ── Grand total across EVERY line we pulled this turn (whether we render
    # it below or not). Useful for "how much did we bill in total" style
    # questions when the data fits inside the LIMIT we used at fetch time.
    grand_net, grand_tax = _invoice_totals(inv)
    lines.append(
        f"\n  GRAND TOTAL across all {len(inv)} line(s) in {len(groups)} "
        f"invoice document(s) pulled: net ${grand_net:,.2f} · tax "
        f"${grand_tax:,.2f} · gross ${grand_net + grand_tax:,.2f}"
    )

    total_lines_shown = 0
    total_lines_skipped = 0
    for idx, (doc, grp) in enumerate(shown_groups):
        first = grp.iloc[0]
        date = str(first.get("OTC_SIH_INVOICE_DATE", "") or "")
        billto = str(first.get("OTC_SIH_BILLTO_NAME", "") or "")
        url = str(first.get("OTC_SIH_INVOICE_URL", "") or "").strip()
        # Tag the FIRST (= most recent) shown invoice explicitly so the LLM
        # can answer "what is the latest bill?" without ambiguity.
        latest_tag = "  ← MOST RECENT INVOICE" if idx == 0 else ""
        lines.append(f"\n  [INVOICE] {doc}  ({date}){latest_tag}  bill-to: {billto}")
        if url and url.lower() != "nan":
            lines.append(f"    URL: {url}")
        else:
            lines.append("    URL: (not provided in SAP for this invoice)")
        # ── Per-invoice totals computed across EVERY line of this invoice
        # (NOT just the lines we render below). When the LLM is asked
        # "what is the total of this bill, with and without tax", it should
        # read this line directly instead of trying to sum the visible
        # lines (which may be capped at max_per_invoice).
        inv_net, inv_tax = _invoice_totals(grp)
        lines.append(
            f"    Totals ({len(grp)} line item(s)): "
            f"net ${inv_net:,.2f} · tax ${inv_tax:,.2f} "
            f"· gross ${inv_net + inv_tax:,.2f}"
        )
        # Per-invoice line cap so a single fat invoice can't crowd out others.
        head_grp = grp.head(max_per_invoice)
        for _, r in head_grp.iterrows():
            mat = str(r.get("OTC_SIL_MATERIAL", "") or "")
            mtxt = str(r.get("OTC_SIL_MATERIAL_TEXT", "") or "")
            qty = str(r.get("OTC_SIL_ORDER_QUANTITY", "") or "")
            net = _fmt_money(r.get("OTC_SIL_NET_AMOUNT"))
            tax = _fmt_money(r.get("OTC_SIL_TAX_AMOUNT"))
            cat = str(r.get("OTC_SIL_ITEM_CATEGORY", "") or "")
            pc = str(r.get("OTC_SIL_PROFIT_CENTER_NAME", "") or "")
            gl = str(r.get("OTC_SIL_MAT_ASSIGN_GL_ACCOUNTDESCRIP", "") or "")
            so = str(r.get("OTC_SIL_SALES_OFFICE_DESCRIPTION", "") or "")
            extra = " · ".join(b for b in (
                f"cat {cat}" if cat and cat != "nan" else "",
                f"qty {qty}" if qty and qty != "nan" else "",
                f"net {net}", f"tax {tax}",
                f"GL: {gl[:30]}" if gl and gl != "nan" else "",
                f"PC: {pc[:30]}" if pc and pc != "nan" else "",
                f"office: {so[:30]}" if so and so != "nan" else "",
            ) if b)
            lines.append(f"      {mat} \"{mtxt[:50]}\" — {extra}")
        total_lines_shown += len(head_grp)
        if len(grp) > len(head_grp):
            extra_lines = len(grp) - len(head_grp)
            total_lines_skipped += extra_lines
            lines.append(f"      (+{extra_lines} more line(s) on this invoice "
                         f"not shown above — full code list below)")
        # ── ALL material codes used on this invoice (compact, every code) ──
        # This is the authoritative list the LLM should use for material-code
        # questions. It covers EVERY line on the invoice, including the ones
        # we suppressed from the full-detail block above.
        lines.append(
            f"    All material codes on this invoice "
            f"({len(grp)} line(s), {grp['OTC_SIL_MATERIAL'].nunique()} "
            f"distinct code(s)):"
        )
        lines.append("      " + _summarize_material_codes(grp))
    if skipped_invoices > 0:
        lines.append(f"\n  (+{skipped_invoices} more invoice document(s) not "
                     f"shown — the {len(shown_groups)} most-recent are above)")
    # Single legacy `len(inv) > max_rows` summary, kept as a fallback.
    if total_lines_skipped == 0 and skipped_invoices == 0 and len(inv) > 0:
        lines.append(f"\n  (showing all {len(inv)} line(s))")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 9.  Diagnostics (used by a small UI status badge / the patch verify cell).
# ─────────────────────────────────────────────────────────────────────────────
def connection_status() -> dict:
    """Return a small dict describing readiness, for the UI. Never raises.

    Fields:
      connector_installed  — snowflake-connector-python importable
      config_found         — a usable snowflake_config.toml was located
      config_source        — path + section it came from
      can_connect          — bare ``SELECT 1`` round-trips
      can_query_table      — the configured view can be probed (LIMIT 0)
      missing_required     — required columns absent from the view (fatal)
      missing_columns      — optional columns absent (will render empty)
      aliased_columns      — {canonical: actual_in_view} that were resolved
      error                — last error message, if any
    """
    status = {
        "connector_installed": snowflake_available(),
        "config_found": False, "config_source": "",
        "can_connect": False, "can_query_table": False,
        "missing_required": [], "missing_columns": [], "aliased_columns": {},
        "error": "",
    }
    if not status["connector_installed"]:
        status["error"] = "snowflake-connector-python not installed"
        return status
    try:
        cfg = _load_cfg()
        status["config_found"] = True
        status["config_source"] = cfg.get("_source", "")
    except Exception as e:
        status["error"] = f"config: {e}"
        return status
    try:
        conn = _get_connection()
        conn.cursor().execute("SELECT 1").fetchone()
        status["can_connect"] = True
    except Exception as e:
        status["error"] = f"connect: {e}"
        return status
    try:
        info = _discover_columns(cfg)
        status["can_query_table"] = True
        status["missing_required"] = info["missing_required"]
        status["missing_columns"] = info["missing"]
        status["aliased_columns"] = info["aliased"]
        if info["missing_required"]:
            status["error"] = ("required columns missing from view: "
                               + ", ".join(info["missing_required"]))
    except Exception as e:
        status["error"] = f"table probe: {e}"
    return status
