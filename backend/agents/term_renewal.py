"""Term & Renewal extractor — initial term, renewal period, auto-renew, notice-to-non-renew."""

from .clause_extractor import ClauseConfig, ClauseExtractor
from config import EXTRACTION_API_KEY

_SYSTEM = (
    "You are a precise contract-reading assistant. From the contract snippets provided, "
    "extract the TERM and RENEWAL clauses. Be conservative — only report values that are "
    "clearly present. If a field is not mentioned in the snippets, return an empty string "
    "for that field. Return STRICT JSON only — no markdown, no commentary."
)

_USER = """\
Contract snippets that mention term / renewal / expiration / auto-renew:

<<snippets_block>>

For this contract, return ONE JSON object (not an array) with these fields:

- "initial_term": initial term length exactly as written (e.g. "5 years", "60 months", "three (3) years"). Empty string if not stated.
- "renewal_period": length of each renewal term (e.g. "3 years", "annual", "one (1) year"). Empty string if not stated.
- "auto_renew": "Yes" if contract auto-renews unless notice given, "No" if renewal requires affirmative action, or "" if unclear.
- "notice_to_non_renew": notice period required to prevent renewal (e.g. "90 days", "180 days prior to expiration"). Empty string if not stated.
- "expiration_or_end_date": specific end date or expiration trigger if stated (e.g. "December 31, 2027", "five years from Effective Date"). Empty string if not stated.
- "source_snippet": the single most relevant snippet that supports your answer (copy exactly, ≤ 300 chars).

Return ONLY the JSON object.
"""

_AGENT = ClauseExtractor(ClauseConfig(
    name="term_renewal",
    display="Term & Renewal",
    search_terms=["term", "renewal", "renew", "expiration", "expire", "expires"],
    search_phrases=["auto-renew", "auto renew", "initial term", "renewal term",
                    "term of this agreement", "shall expire"],
    context_window=70,
    api_key=EXTRACTION_API_KEY,
    model="gpt-4o-mini",
    system_prompt=_SYSTEM,
    user_prompt=_USER,
    field_mapping={
        "initial_term":          "Initial Term",
        "renewal_period":        "Renewal Period",
        "auto_renew":            "Auto-Renew",
        "notice_to_non_renew":   "Notice to Non-Renew",
        "expiration_or_end_date": "Expiration / End Date",
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
