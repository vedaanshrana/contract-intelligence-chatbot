"""
Lightweight, process-local collector for per-agent run metrics:
runtime, LLM model(s), and input/output tokens.

How it fits together
--------------------
* `fiserv_client.make_client()` returns a metered client whose
  `chat.completions.create()` / `responses.create()` calls invoke
  `record_call(...)` after every LLM round-trip.  This captures token usage
  for ALL agents centrally (every agent builds its client via make_client).

* Each agent runner in chatbot.py brackets the agent with
  `mark = snapshot()` ... `finalize(client, agent, ...)`.  finalize measures
  wall-clock runtime, sums the tokens recorded since `mark`, figures out which
  model(s) were used, and appends one record to
  `Output/<client>/run_metrics.json`.

Because metrics are summed over "calls since mark", this works whether the
user runs all 9 agents or any subset — each runner only ever counts its own
slice of calls.

Token counts depend on the backend returning a usage block.  The OpenAI SDK
always does; the Fiserv Foundation gateway returns usage when it includes one
(0 is shown if it doesn't).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import OUTPUT_DIR

_lock = threading.Lock()
# In-memory tape of individual LLM calls for the current process/session.
# Each entry: {"model": str, "input": int, "output": int, "total": int}
_calls: list[dict] = []


# ── Recording (called by the metered client wrapper) ──────────────────────────
def record_call(model: Optional[str],
                input_tokens, output_tokens, total_tokens=None,
                actual_model: Optional[str] = None) -> None:
    """Append one LLM call's usage to the tape. Safe to call with missing/None
    values — they're coerced to 0.

    ``model``        — the model name the calling code *requested* (e.g. what
                       was passed to ``client.chat.completions.create(model=…)``).
    ``actual_model`` — the model name the API actually *used*, as echoed in
                       ``response.model``. On the Fiserv VDI proxy these
                       routinely differ — the proxy is bound to whatever model
                       is configured behind the X-Purpose tag, and the
                       requested name is just a routing hint. We store both so
                       the Run Details tab can flag the discrepancy."""
    try:
        it = int(input_tokens or 0)
    except (TypeError, ValueError):
        it = 0
    try:
        ot = int(output_tokens or 0)
    except (TypeError, ValueError):
        ot = 0
    try:
        tt = int(total_tokens) if total_tokens else it + ot
    except (TypeError, ValueError):
        tt = it + ot
    with _lock:
        _calls.append({
            "model":        (model or ""),
            "actual_model": (actual_model or ""),
            "input":        it,
            "output":       ot,
            "total":        tt,
        })


# ── Bracketing helpers (called by the chatbot runners) ────────────────────────
def snapshot() -> int:
    """Return a marker for the current end of the call tape."""
    with _lock:
        return len(_calls)


def collect_since(mark: int) -> dict:
    """Aggregate all calls recorded since `mark`.

    Returns both the legacy aggregate (input/output/total tokens summed
    across all models) AND a `per_model` breakdown so callers can compute
    cost per model. Each bucket in `per_model` carries:

      • calls / input / output / total — token counts
      • requested_models — sorted list of unique requested names that landed
                          in this bucket (typically one entry; multiple when
                          the proxy collapsed several requested names to a
                          single actual model)
      • actual_models    — sorted list of unique actual names (typically one;
                          multiple when the proxy returned different actuals
                          for the same requested name across calls)

    Bucket key: the **actual model** returned by the API when echoed, else
    the requested model when actual is empty, else "(unknown)". This means
    rolling up the metrics tells you what models actually served the work —
    which is the truth the Foundation API proxy can hide if you only look
    at the request side."""
    with _lock:
        chunk = _calls[mark:]
    input_t  = sum(c["input"]  for c in chunk)
    output_t = sum(c["output"] for c in chunk)
    total_t  = sum(c["total"]  for c in chunk)

    # The aggregate label for back-compat with the older 'model' field on
    # the run record. Prefer actual when present (per call), else requested.
    labels: list[str] = []
    for c in chunk:
        labels.append(c.get("actual_model") or c.get("model") or "")
    labels = [m for m in labels if m]
    if labels:
        uniq = sorted(set(labels))
        model_label = uniq[0] if len(uniq) == 1 else " + ".join(uniq)
    else:
        model_label = ""

    per_model: dict[str, dict] = {}
    for c in chunk:
        req    = c.get("model") or ""
        actual = c.get("actual_model") or ""
        # Bucket key — see docstring above.
        key = actual or req or "(unknown)"
        bucket = per_model.setdefault(key, {
            "calls": 0, "input": 0, "output": 0, "total": 0,
            "requested_models": set(), "actual_models": set(),
        })
        bucket["calls"]  += 1
        bucket["input"]  += c["input"]
        bucket["output"] += c["output"]
        bucket["total"]  += c["total"]
        if req:
            bucket["requested_models"].add(req)
        if actual:
            bucket["actual_models"].add(actual)

    # Convert sets to sorted lists for JSON-serialisability.
    for v in per_model.values():
        v["requested_models"] = sorted(v["requested_models"])
        v["actual_models"]    = sorted(v["actual_models"])

    return {"calls": len(chunk), "input_tokens": input_t,
            "output_tokens": output_t, "total_tokens": total_t,
            "model": model_label, "per_model": per_model}


def reset() -> None:
    """Clear the in-memory tape (called after each finalize to bound memory)."""
    with _lock:
        _calls.clear()


# ── Persistence (one JSON file per client) ────────────────────────────────────
def _metrics_path(client: str) -> Path:
    return OUTPUT_DIR / client / "run_metrics.json"


def load_runs(client: str) -> list[dict]:
    """Return all recorded run records for a client (oldest first)."""
    p = _metrics_path(client)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def latest_by_agent(client: str) -> dict[str, dict]:
    """Return the most recent run record per agent for a client."""
    latest: dict[str, dict] = {}
    for rec in load_runs(client):
        latest[rec.get("agent", "")] = rec   # later entries overwrite -> newest wins
    return latest


def log_run(client: str, agent: str, display: str, runtime_s: float,
            model: str, input_tokens: int, output_tokens: int,
            total_tokens: int, calls: int = 0,
            status: str = "complete",
            per_model: Optional[dict] = None) -> dict:
    """Append one run record to Output/<client>/run_metrics.json and return it.

    `per_model` (when supplied) is a dict {model_name: {calls, input, output,
    total}} that the Run Details tab uses to compute cost per model.
    Backwards-compatible: older records that lack this field still load fine."""
    rec = {
        "agent":        agent,
        "display":      display,
        "timestamp":    datetime.now().isoformat(timespec="seconds"),
        "runtime_s":    round(float(runtime_s), 2),
        "model":        model or "",
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "calls":        int(calls or 0),
        "status":       status,
        "per_model":    per_model or {},
    }
    p = _metrics_path(client)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = load_runs(client)
    data.append(rec)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return rec


def finalize(client: str, agent: str, display: str, t0: float, mark: int,
             fallback_model: str = "", status: str = "complete") -> dict:
    """Measure runtime since `t0`, sum tokens since `mark`, persist a run record,
    then clear the in-memory tape. Call this in a `finally:` around an agent run."""
    runtime_s = time.perf_counter() - t0
    u = collect_since(mark)
    model = u["model"] or fallback_model

    # If we have no model info from actual calls (Hierarchy used to land here
    # because it bypassed make_client; that's fixed now, but if a future
    # backend skips usage echoes the per_model dict will key on the fallback).
    per_model = u.get("per_model") or {}
    if not per_model and fallback_model and (u["input_tokens"] or u["output_tokens"]):
        per_model = {fallback_model: {
            "calls":            u["calls"],
            "input":            u["input_tokens"],
            "output":           u["output_tokens"],
            "total":            u["total_tokens"],
            "requested_models": [fallback_model],
            "actual_models":    [],
        }}

    rec = log_run(client, agent, display, runtime_s, model,
                  u["input_tokens"], u["output_tokens"], u["total_tokens"],
                  calls=u["calls"], status=status,
                  per_model=per_model)
    reset()
    return rec
