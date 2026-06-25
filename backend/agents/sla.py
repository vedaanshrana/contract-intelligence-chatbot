"""SLA & service-credit extractor — uptime commitments, credit formulas, response times."""

from .clause_extractor import ClauseConfig, ClauseExtractor
from config import EXTRACTION_API_KEY

_SYSTEM = (
    "You are a precise contract-reading assistant. From the snippets provided, extract the "
    "SERVICE LEVEL AGREEMENT (SLA) terms — uptime guarantees, service credits, response times. "
    "Be conservative — only report values clearly present. Return STRICT JSON only."
)

_USER = """\
Contract snippets that mention SLAs, service levels, uptime, availability, credits, or response times:

<<snippets_block>>

Return ONE JSON object with these fields:

- "uptime_commitment": uptime / availability % (e.g. "99.9%", "99.5% measured monthly"). Empty string if not stated.
- "service_credit_formula": how credits are calculated (e.g. "5% of monthly fee per hour of downtime, capped at 25%"). Empty string if not stated.
- "response_time": response time commitments for incidents/support (e.g. "Severity 1: 1 hour, Severity 2: 4 hours"). Empty string if not stated.
- "resolution_time": resolution time commitments if separate from response (e.g. "Severity 1 resolved in 4 hours"). Empty string if not stated.
- "covered_services": which products/services the SLA covers (e.g. "Online Banking, Mobile Banking"). Empty string if not stated.
- "source_snippet": the single most relevant snippet (copy exactly, ≤ 300 chars).

Return ONLY the JSON object.
"""

_AGENT = ClauseExtractor(ClauseConfig(
    name="sla",
    display="SLA & Service Credits",
    search_terms=["uptime", "availability", "downtime", "outage"],
    search_phrases=["service level", "service-level", "service level agreement",
                    "service credit", "service credits", "response time",
                    "resolution time", "incident", "severity 1", "severity level"],
    context_window=80,
    api_key=EXTRACTION_API_KEY,
    model="gpt-4o-mini",
    system_prompt=_SYSTEM,
    user_prompt=_USER,
    field_mapping={
        "uptime_commitment":     "Uptime Commitment",
        "service_credit_formula": "Service Credit Formula",
        "response_time":         "Response Time",
        "resolution_time":       "Resolution Time",
        "covered_services":      "Covered Services",
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
