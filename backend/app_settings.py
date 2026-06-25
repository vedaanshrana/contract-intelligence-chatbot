"""Persisted, process-wide application settings for the API backend.

Replaces the per-session ``st.session_state`` model/dictionary/year settings
the Streamlit UI used to hold. Stored as a small JSON file next to the code so
the React Settings panel can read and update them, and so a restart keeps the
last-applied configuration.

Everything here is deliberately tiny and dependency-free — it's just the knobs
the agents and chat engine already accept as arguments, surfaced over HTTP.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import config

_LOCK = threading.Lock()
_PATH = Path(__file__).parent / "server_settings.json"

# Defaults mirror the values the Streamlit sidebar seeded into session_state,
# which in turn matched config.py. Keeping them here means a fresh install
# behaves identically to the old UI on first boot.
_DEFAULTS: dict = {
    "chat_model":       config.CHAT_MODEL,            # gpt-4.1-2025-04-14
    "hier_model":       config.HIERARCHY_MODEL,       # gpt-5.2
    "extr_model":       config.EXTRACTION_MODEL,      # gpt-5.2-2025-12-11
    "match_model":      config.MATCHING_MODEL,        # gpt-4.1-2025-04-14
    "cpi_model":        config.CPI_MODEL,             # gpt-4o-mini
    "engagement_model": config.MASTER_CONTRACT_MODEL,  # gpt-5.2-2025-12-11
    "scope_model":      config.SCOPE_AGENT_MODEL,     # gpt-4o-mini
    # Empty string => use the Core's default dictionary (config.default_dictionary_for).
    "dict_path":        "",
    # PDF year cutoff used by the Fee Description agent at discovery time.
    "min_year":         2022,
}


def _read() -> dict:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def get_all() -> dict:
    """Return the full settings dict (defaults overlaid with any saved values)."""
    with _LOCK:
        merged = {**_DEFAULTS, **_read()}
    return merged


def get(key: str):
    return get_all().get(key)


def update(patch: dict) -> dict:
    """Merge ``patch`` into the saved settings and return the new full dict.

    Only known keys are accepted so a typo in the UI can't inject garbage that
    later flows into an agent's ``model=`` argument."""
    with _LOCK:
        current = {**_DEFAULTS, **_read()}
        for k, v in (patch or {}).items():
            if k in _DEFAULTS:
                current[k] = v
        _PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
        return current
