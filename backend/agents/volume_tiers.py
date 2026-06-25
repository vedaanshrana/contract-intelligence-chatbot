"""Volume tier / minimum commitment extractor."""

from .clause_extractor import ClauseConfig, ClauseExtractor
from config import EXTRACTION_API_KEY

_SYSTEM = (
    "You are a precise contract-reading assistant. From the snippets provided, extract any "
    "VOLUME-BASED PRICING, TIER BREAKPOINTS, MINIMUM COMMITMENTS, or TRUE-UP terms. "
    "Be conservative. Return STRICT JSON only — no markdown, no commentary."
)

_USER = """\
Contract snippets that mention minimums, volume tiers, breakpoints, thresholds, or true-ups:

<<snippets_block>>

Return ONE JSON object with these fields:

- "minimum_commitment": minimum dollar or volume commitment per period (e.g. "$10,000 / month minimum", "50,000 accounts"). Empty string if not stated.
- "volume_tiers": tier breakpoints + per-unit pricing if stated (e.g. "0–10K accounts: $0.50; 10K–50K: $0.40; >50K: $0.30"). Empty string if not stated.
- "tier_basis": what the tiering is based on (e.g. "active accounts", "transactions per month", "total assets under management"). Empty string if not stated.
- "true_up_cadence": frequency of true-up / reconciliation (e.g. "Annual true-up", "Quarterly review"). Empty string if not stated.
- "overage_charges": charges for exceeding tier thresholds (e.g. "$0.05 per transaction over 100K/month"). Empty string if not stated.
- "source_snippet": the single most relevant snippet (copy exactly, ≤ 300 chars).

Return ONLY the JSON object.
"""

_AGENT = ClauseExtractor(ClauseConfig(
    name="volume_tiers",
    display="Volume Tiers & Minimums",
    search_terms=["minimum", "tier", "threshold", "overage"],
    search_phrases=["volume tier", "tiered pricing", "minimum commitment",
                    "minimum monthly", "minimum annual", "true-up", "true up",
                    "in excess of", "per account", "per transaction", "per item"],
    context_window=80,
    api_key=EXTRACTION_API_KEY,
    model="gpt-4o-mini",
    system_prompt=_SYSTEM,
    user_prompt=_USER,
    field_mapping={
        "minimum_commitment": "Minimum Commitment",
        "volume_tiers":       "Volume Tiers",
        "tier_basis":         "Tier Basis",
        "true_up_cadence":    "True-Up Cadence",
        "overage_charges":    "Overage Charges",
    },
))


def run(client_name: str, api_key: str = "", progress_callback=None,
        contracts=None, core: str = "") -> dict:
    return _AGENT.extract(client_name, api_key=api_key,
                          progress_callback=progress_callback,
                          contracts=contracts, core=core)


def is_processed(client_name: str) -> bool:
    return _AGENT.is_processed(client_name)


def output_path(client_name: str):
    return _AGENT.output_path(client_name)
