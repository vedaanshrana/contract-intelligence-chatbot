"""
Back-compat shim — ``agents.master_contract`` was renamed to
``agents.engagement_overview``.

This module re-exports everything from the new location so any caller that
still imports from ``agents.master_contract`` keeps working without changes.
Safe to delete once you're sure nothing in the project references the old
name.
"""

from .engagement_overview import *           # noqa: F401, F403
from .engagement_overview import (           # noqa: F401  (explicit re-exports)
    run,
    is_processed,
    output_path,
    Phase1Scope,
    ScheduleManifest,
    ScheduleManifestItem,
    Phase1SowSupplement,
    OUTPUT_COLS,
    pdf_to_images,
    select_phase1_pages,
    call_vision,
    run_phase1,
    run_phase1_5_manifest,
    run_phase1_sow_supplement,
    apply_license_override,
)

# Legacy alias — the previous master_contract.py exposed this path helper for
# the Phase-2 product hierarchy file. Keep it pointing at the new agent.
def product_hierarchy_path(client_name: str):
    from . import product_module
    return product_module.output_path(client_name)
