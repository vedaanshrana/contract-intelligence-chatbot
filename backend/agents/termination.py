"""Termination clause extractor — for cause / for convenience, notice periods, early-termination fees."""

from .clause_extractor import ClauseConfig, ClauseExtractor
from config import EXTRACTION_API_KEY

_SYSTEM = (
    "You are a precise contract-reading assistant. From the snippets provided, extract the "
    "TERMINATION clauses. Be conservative — only report what's clearly stated. Return STRICT JSON "
    "only — no markdown, no commentary."
)

_USER = """\
Contract snippets that mention termination, notice, breach, or default:

<<snippets_block>>

Return ONE JSON object with these fields:

- "termination_for_cause": short summary of grounds + notice required (e.g. "Material breach uncured after 30 days"). Empty string if not stated.
- "termination_for_convenience": "Yes" / "No" / "" + notice period if applicable (e.g. "Yes — 180 days notice").
- "early_termination_fee": dollar value, formula, or textual indicator (e.g. "$50,000", "remaining fees through term", "Waived"). Empty string if not stated.
- "notice_period": general notice period the contract requires to terminate (e.g. "90 days written notice"). Empty string if not stated.
- "survival_clauses": which sections / obligations survive termination (e.g. "Confidentiality, Indemnity, Payment of fees due"). Empty string if not stated.
- "source_snippet": the single most relevant snippet (copy exactly, ≤ 300 chars).

Return ONLY the JSON object.
"""

_AGENT = ClauseExtractor(ClauseConfig(
    name="termination",
    display="Termination",
    search_terms=["termination", "terminate", "terminated", "breach", "default"],
    search_phrases=["termination for cause", "termination for convenience",
                    "early termination", "early termination fee", "notice of termination",
                    "right to terminate", "may terminate", "shall survive"],
    context_window=80,
    api_key=EXTRACTION_API_KEY,
    model="gpt-4o-mini",
    system_prompt=_SYSTEM,
    user_prompt=_USER,
    field_mapping={
        "termination_for_cause":       "Termination for Cause",
        "termination_for_convenience": "Termination for Convenience",
        "early_termination_fee":       "Early Termination Fee",
        "notice_period":               "Notice Period",
        "survival_clauses":            "Survival Clauses",
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
