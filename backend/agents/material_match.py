"""
Material Code Matching Agent — frontend re-export.

The actual implementation is in `agents/extraction.py`. Both phases of the
colleague's reference notebook (Phase 1 extraction + Phase 2 matching) live
in that single file, with all imports at the top exactly as in the
original. The chatbot's frontend exposes two agents — Fee Description and
Material Code Matching — but they're just two entry points into the same
backend module.

This file exists ONLY so the existing frontend code (`agents.material_match`
imports in chatbot.py + context_builder.py + snowflake_invoice.py) keeps
working without modification.

Re-exports:
    run               → extraction.run_matching          (Phase 2 entry)
    output_path       → extraction.matching_output_path  (Phase 2 output)
    is_processed      → extraction.matching_is_processed (Phase 2 done?)
"""
from .extraction import (
    run_matching       as run,
    matching_output_path as output_path,
    matching_is_processed as is_processed,
    # Optional exports — handy for downstream code that wants the prompt /
    # constants directly (e.g. for tests or notebooks):
    MATCHING_SYSTEM_PROMPT,
    MATCHING_MODEL,
    ITEM_BATCH_SIZE,
    DICT_CHUNK_SIZE,
    MAX_PARALLEL_CALLS,
    FUZZY_AUTO_ACCEPT_THRESHOLD,
    SEMANTIC_WEIGHT,
    LEXICAL_WEIGHT,
    _VERSION,
)

__all__ = [
    "run", "output_path", "is_processed",
    "MATCHING_SYSTEM_PROMPT", "MATCHING_MODEL",
    "ITEM_BATCH_SIZE", "DICT_CHUNK_SIZE", "MAX_PARALLEL_CALLS",
    "FUZZY_AUTO_ACCEPT_THRESHOLD", "SEMANTIC_WEIGHT", "LEXICAL_WEIGHT",
    "_VERSION",
]
