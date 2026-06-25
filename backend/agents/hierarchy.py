"""
Hierarchy agent — adapter around the standalone Contract Hierarchy Analyzer.

The heavy lifting (vision-LLM metadata extraction, parent/child resolution
across five strategies, product canonicalisation, Plotly HTML viz, styled
Excel) lives in ``Existing Scripts/contract_hierarchy_analyzer.py`` (~5900
lines).  This adapter is the chatbot-facing wrapper: it loads that script
once, points its module-level globals at the chatbot's folder layout,
swaps in the metered ``fiserv_client.make_client()`` so token usage is
recorded, and drives the standalone ``main()`` over one client at a time.

What the underlying script produces, per client:
  Output/<Client>/contracts_hierarchy.xlsx    — styled multi-sheet report
  Output/<Client>/contracts_hierarchy.html    — Plotly interactive graph
  Output/hierarchy_cache.json                  — extraction cache, keyed by
                                                 "<Client>/<Filename>"

Backend portability — ``fiserv_client.make_client()`` returns either the
OpenAI SDK (OPENAI_BACKEND=openai) or the Fiserv FoundationClient shim
(OPENAI_BACKEND=fiserv). The standalone script calls
``client.responses.create(...)``; both backends support that shape.

The chatbot's downstream readers (``context_builder.build_hierarchy_context``
and the Agent Outputs panel) consume the same cache JSON + per-client Excel
the underlying script writes, so the rest of the app doesn't change.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from threading import Lock
from typing import Callable, Optional

import pandas as pd

from fiserv_client import make_client
from config import INPUT_DIR, OUTPUT_DIR, HIERARCHY_MODEL

# HIERARCHY_API_KEY was added in the hierarchy-rewrite patch. Some older
# config.py versions don't expose it yet — fall back to MASTER_CONTRACT_API_KEY
# → EXTRACTION_API_KEY so the agent still imports cleanly.
try:
    from config import HIERARCHY_API_KEY                        # type: ignore
except ImportError:
    try:
        from config import MASTER_CONTRACT_API_KEY as HIERARCHY_API_KEY  # type: ignore
    except ImportError:
        from config import EXTRACTION_API_KEY as HIERARCHY_API_KEY       # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Locate and lazily-load the standalone analyzer module.
# ─────────────────────────────────────────────────────────────────────────────
_REPO_ROOT       = Path(__file__).resolve().parent.parent
_ANALYZER_PATH   = _REPO_ROOT / "Existing Scripts" / "contract_hierarchy_analyzer.py"
_CACHE_FILENAME  = "hierarchy_cache.json"      # chatbot's historical cache name

_analyzer_mod_lock = Lock()
_analyzer_mod = None                            # populated on first call


def _load_analyzer():
    """Load (or return the cached) standalone analyzer module."""
    global _analyzer_mod
    with _analyzer_mod_lock:
        if _analyzer_mod is not None:
            return _analyzer_mod
        if not _ANALYZER_PATH.exists():
            raise FileNotFoundError(
                f"Hierarchy: standalone analyzer not found at {_ANALYZER_PATH}. "
                f"It should sit at <project>/Existing Scripts/contract_hierarchy_analyzer.py."
            )
        spec = importlib.util.spec_from_file_location(
            "_hierarchy_analyzer", str(_ANALYZER_PATH))
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load analyzer spec from {_ANALYZER_PATH}")
        mod = importlib.util.module_from_spec(spec)
        # The script imports tenacity / plotly / openpyxl.styles at module load —
        # those are in requirements.txt; let any ImportError surface up.
        spec.loader.exec_module(mod)
        _analyzer_mod = mod
        return mod


# ─────────────────────────────────────────────────────────────────────────────
# Stdout fan-out: redirect the analyzer's prints into the chatbot's
# progress_callback (and the back-compat progress_lines list) line-by-line.
# ─────────────────────────────────────────────────────────────────────────────

class _LineFanout(io.TextIOBase):
    """File-like that splits writes into lines and forwards each line to the
    user-provided log callback. Keeps an in-memory buffer for the trailing
    partial line (the analyzer does `print(..., end="")` then a later
    `print(" done")` — we don't want to drop the prefix)."""

    def __init__(self, log: Callable[[str], None]):
        super().__init__()
        self._log = log
        self._buf = ""

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            try:
                self._log(line)
            except Exception:
                pass
        return len(s)

    def flush(self) -> None:
        if self._buf:
            try:
                self._log(self._buf)
            except Exception:
                pass
            self._buf = ""


# ─────────────────────────────────────────────────────────────────────────────
# Top-level entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(
    client_name: str,
    api_key: str = "",
    hierarchy_model: str = "",
    progress_callback: Optional[Callable[[str], None]] = None,
    progress_lines:   Optional[list] = None,
    contracts:        Optional[list] = None,
    core: str = "",
    force: bool = False,
) -> dict:
    """Run the full Contract Hierarchy Analyzer pipeline for ``client_name``.

    Produces per the standalone script:
      Output/<client_name>/contracts_hierarchy.xlsx
      Output/<client_name>/contracts_hierarchy.html
      Output/hierarchy_cache.json                          (chatbot cache name)

    Args:
      client_name        — folder name under Input/<core>/.
      api_key            — OpenAI key; falls back to HIERARCHY_API_KEY. Ignored
                            on the Fiserv backend.
      hierarchy_model    — model name; falls back to HIERARCHY_MODEL.
      progress_callback  — invoked with each stdout line (Streamlit hook).
      progress_lines     — list to extend with log lines (back-compat).
      contracts          — optional whitelist of filenames. When provided, the
                            analyzer's scan is filtered to those filenames
                            after the per-client scan.
      core               — Core folder name (PORTICO / DNA / …) so the input
                            folder resolves to ``Input/<core>/<client_name>``.
      force              — when True, wipe this client's cache entries before
                            running so every contract is re-extracted.

    Returns: a dict with status, client, rows, excel, html, cache, elapsed_s.
    Raises if the input folder doesn't exist or the analyzer can't be loaded.
    """
    # ── Fan-out for stdout AND optional callback lists ───────────────────────
    def log(msg: str) -> None:
        if progress_callback:
            try:
                progress_callback(msg)
            except Exception:
                pass
        if progress_lines is not None:
            progress_lines.append(msg)

    api_key = api_key or HIERARCHY_API_KEY
    model   = hierarchy_model or HIERARCHY_MODEL

    backend = (os.environ.get("OPENAI_BACKEND") or "openai").lower()
    if backend == "openai" and not api_key:
        raise RuntimeError(
            "Hierarchy: OPENAI_BACKEND=openai but no API key available. "
            "Set HIERARCHY_API_KEY (or EXTRACTION_API_KEY) in .env or the "
            "environment before launching the chatbot."
        )

    # ── Validate input folder up-front ───────────────────────────────────────
    folder = (INPUT_DIR / core / client_name) if core else (INPUT_DIR / client_name)
    if not folder.exists():
        raise RuntimeError(
            f"Hierarchy: input folder does not exist: {folder}. "
            f"Check that Input/{core or '<client>'}/{client_name}/ contains contract files."
        )

    contracts_root = (INPUT_DIR / core) if core else INPUT_DIR
    output_root    = OUTPUT_DIR
    cache_path     = OUTPUT_DIR / _CACHE_FILENAME

    # ── Load the analyzer module (cached after first call) ──────────────────
    mod = _load_analyzer()

    # ── Optional force-refresh: wipe this client's entries from the cache ───
    if force and cache_path.exists():
        try:
            existing = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        prefix = f"{client_name}/"
        before = len(existing)
        existing = {k: v for k, v in existing.items() if not k.startswith(prefix)}
        if len(existing) != before:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
            log(f"  force=True → evicted {before - len(existing)} cache "
                f"entries for {client_name!r}")

    # ── Patch the analyzer's module globals to point at chatbot paths ────────
    saved = {
        "OPENAI_API_KEY":   getattr(mod, "OPENAI_API_KEY", ""),
        "OPENAI_MODEL":     getattr(mod, "OPENAI_MODEL",   ""),
        "CONTRACTS_ROOT":   getattr(mod, "CONTRACTS_ROOT", ""),
        "OUTPUT_DIR":       getattr(mod, "OUTPUT_DIR",     ""),
        "CACHE_FILE":       getattr(mod, "CACHE_FILE",     ""),
        "ONLY_CLIENTS":     getattr(mod, "ONLY_CLIENTS",   None),
        "INGEST_FLAT_DIR":  getattr(mod, "INGEST_FLAT_DIR", ""),
        "_openai_client":   getattr(mod, "_openai_client", None),
        "get_openai_client":getattr(mod, "get_openai_client", None),
    }

    # On the Fiserv backend the API key is unused (auth via headers) — but the
    # analyzer's main() guards on `not OPENAI_API_KEY or OPENAI_API_KEY.startswith("sk-...")`
    # and bails out silently, so feed it a sentinel that passes the guard.
    effective_key = api_key if (backend == "openai") else (api_key or "VDI-no-direct-key")

    mod.OPENAI_API_KEY  = effective_key
    mod.OPENAI_MODEL    = model
    mod.CONTRACTS_ROOT  = str(contracts_root)
    mod.OUTPUT_DIR      = str(output_root)
    mod.CACHE_FILE      = str(cache_path)
    mod.ONLY_CLIENTS    = [client_name]
    mod.INGEST_FLAT_DIR = ""                     # force per-client subfolder mode

    # Reset the cached OpenAI client so the override below takes effect for
    # this run even if a previous run already populated it.
    mod._openai_client = None

    # Replace get_openai_client() so every call inside the analyzer goes
    # through the metered chatbot client (token usage recorded automatically
    # via run_metrics.record_call). make_client() returns either the real
    # OpenAI SDK or the Fiserv FoundationClient — both expose .responses.create.
    metered = make_client(effective_key or None, timeout=180)

    def _get_openai_client_override():
        return metered

    mod.get_openai_client = _get_openai_client_override

    # Some main()-internal code re-reads sys.argv to pick up an alternate
    # client filter. Stub it out so ONLY_CLIENTS wins inside Streamlit.
    saved_argv = list(sys.argv)
    sys.argv = [saved_argv[0]] if saved_argv else ["hierarchy"]

    log(f"Hierarchy agent — running for {client_name!r} "
        f"(backend={backend}, model={model})")
    log(f"  contracts_root = {contracts_root}")
    log(f"  output_root    = {output_root}")
    log(f"  cache_file     = {cache_path}")

    fanout = _LineFanout(log)
    t_start = time.perf_counter()
    err_msg = None
    try:
        with redirect_stdout(fanout), redirect_stderr(fanout):
            mod.main()
        fanout.flush()
    except SystemExit as e:
        # The analyzer's main() never raises SystemExit normally, but defend
        # against it (some older versions called sys.exit on guards).
        err_msg = f"main() exited with {e.code}"
    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}"
        log(f"  ✗ Hierarchy run failed: {err_msg}")
        log(traceback.format_exc())
    finally:
        sys.argv = saved_argv
        # Restore the patched globals so a second client's run starts clean
        # (the cached metered client survives by design — we want it reused).
        for k, v in saved.items():
            try:
                setattr(mod, k, v)
            except Exception:
                pass
        # But the next call to run() will set them again immediately.

    elapsed = time.perf_counter() - t_start

    excel_path = output_root / client_name / "contracts_hierarchy.xlsx"
    html_path  = output_root / client_name / "contracts_hierarchy.html"

    # Count rows for the response payload (cache + Excel are persisted to
    # disk by the analyzer itself).
    rows = 0
    if excel_path.exists():
        try:
            rows = len(pd.read_excel(str(excel_path)))
        except Exception:
            rows = 0

    if err_msg or not excel_path.exists():
        return {
            "status":      "error" if err_msg else "incomplete",
            "client":      client_name,
            "rows":        rows,
            "extracted":   rows,
            "cached":      0,
            "failed":      0 if rows else 1,
            "excel":       str(excel_path) if excel_path.exists() else "",
            "html":        str(html_path) if html_path.exists() else "",
            "cache":       str(cache_path) if cache_path.exists() else "",
            "elapsed_s":   round(elapsed, 2),
            "error":       err_msg or "no Excel written",
        }

    log(f"Hierarchy done in {elapsed:.1f}s — {rows} contract row(s) written.")
    return {
        "status":      "complete",
        "client":      client_name,
        "rows":        rows,
        "extracted":   rows,
        "cached":      0,           # standalone main() doesn't split this out
        "failed":      0,
        "excel":       str(excel_path),
        "html":        str(html_path),
        "cache":       str(cache_path),
        "elapsed_s":   round(elapsed, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Compatibility helpers (consumed by chatbot.py / context_builder.py)
# ─────────────────────────────────────────────────────────────────────────────

def is_processed(client_name: str) -> bool:
    """True iff this client has at least one cached extraction AND the per-
    client Excel exists on disk. Stricter than 'just one cache hit' so the
    UI doesn't badge a half-finished run as Done."""
    cache_path = OUTPUT_DIR / _CACHE_FILENAME
    excel_path = OUTPUT_DIR / client_name / "contracts_hierarchy.xlsx"
    if not cache_path.exists() or not excel_path.exists():
        return False
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        return any(k.startswith(f"{client_name}/") for k in cache)
    except Exception:
        return False


def output_path(client_name: str) -> Path:
    return OUTPUT_DIR / client_name / "contracts_hierarchy.xlsx"


def html_path(client_name: str) -> Path:
    """Path to the Plotly interactive graph the analyzer writes per client."""
    return OUTPUT_DIR / client_name / "contracts_hierarchy.html"


def load_results(client_name: str) -> Optional[dict]:
    """Return {"cache": <per-client cache>, "df": <Excel df>, "client": …}
    or None when nothing has been extracted yet. Same shape the older
    chatbot adapter exposed."""
    cache_path = OUTPUT_DIR / _CACHE_FILENAME
    if not cache_path.exists():
        return None
    try:
        full_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    client_cache = {k: v for k, v in full_cache.items()
                    if k.startswith(f"{client_name}/")}
    if not client_cache:
        return None

    result: dict = {"cache": client_cache, "client": client_name, "df": None}
    excel = OUTPUT_DIR / client_name / "contracts_hierarchy.xlsx"
    if excel.exists():
        try:
            result["df"] = pd.read_excel(str(excel))
        except Exception:
            pass
    return result