"""
Drop-in OpenAI-SDK-shaped wrapper that routes every call through the Fiserv
Foundation API (the internal LLM gateway available inside the Fiserv VDI).

Why this exists
---------------
The Foundation API speaks a chat/completions-style protocol with custom headers
(X-Purpose, X-Session-Id, X-Source, X-Email-Id) — no `Authorization: Bearer`,
no api.openai.com.  Inside the VDI, calls to api.openai.com are firewalled,
which is why extraction + matching stop working there.

Model selection via X-Purpose
-----------------------------
The gateway deployment is always "default"; the actual model is chosen by the
`X-Purpose` header, NOT by a `model` field in the payload.  We therefore map the
`model` each agent requests to the right purpose tag, per call:

    GPT-5-family model (e.g. "gpt-5.1", "gpt-5.2-2025-12-11")  ->  GPT5.1Purpose
    everything else    (gpt-4.1, gpt-4o-mini, …)               ->  GPT4.1Purpose

Both tags are overridable via env (FISERV_PURPOSE_GPT5 / FISERV_PURPOSE_GPT4).

This module exposes a `FoundationClient` class that has the same surface our
agents already use:

    client = FoundationClient(api_key="unused-in-vdi", email="me@fiserv.com")

    # chat.completions (used by chat_engine, cpi, scope_agent, clause_extractor)
    client.chat.completions.create(model=…, messages=[...], temperature=0,
                                    max_tokens=800)

    # responses (used by extraction, master_contract for vision passes)
    client.responses.create(model=…, input=[...], text={...},
                            max_output_tokens=…)

The `responses.create` shim translates OpenAI Responses-API content blocks
(`input_text`, `input_image`) into Foundation API's chat/completions content
blocks (`text`, `image_url`).  Strict JSON-schema mode degrades to JSON mode
(Foundation API doesn't expose full schema enforcement); callers that need
strict schema validate the JSON themselves.

To switch the whole app to the Fiserv backend, set in `.env` or the shell:

    OPENAI_BACKEND=fiserv
    FOUNDATION_API_URL=https://dev-cst-cognitive-service.onefiserv.net/FoundationAPI/openai/deployments/default/chat/completions?api-version=2025-03-01-preview
    FISERV_EMAIL=your.name@fiserv.com
    FISERV_PURPOSE_GPT5=GPT5.1Purpose   # optional, default GPT5.1Purpose
    FISERV_PURPOSE_GPT4=GPT4.1Purpose   # optional, default GPT4.1Purpose

When `OPENAI_BACKEND=openai` (or unset), `make_client()` returns the real
OpenAI SDK so the same code runs on a laptop with internet access.
"""

import json
import os
import re
import uuid
from typing import Optional

import httpx


# ── Public factory ──────────────────────────────────────────────────────────

def make_client(api_key: Optional[str] = None, **kwargs):
    """Return either an `openai.OpenAI` instance or a `FoundationClient`
    depending on the `OPENAI_BACKEND` environment variable.

    Every agent should call this instead of instantiating `OpenAI(...)`
    directly so the backend is centrally controlled.

    Extra **kwargs (e.g. ``timeout=180``) are forwarded to the underlying
    constructor when meaningful for the chosen backend.  The hierarchy
    adapter relies on ``timeout=`` being honoured so a hung request bounces
    to the next retry within a bounded time instead of consuming the SDK's
    10-minute default.
    """
    backend = (os.environ.get("OPENAI_BACKEND") or "openai").lower()
    if backend == "fiserv":
        # FoundationClient applies timeouts at the per-call level (.create
        # takes a timeout=); a connection-level timeout has no equivalent
        # in the gateway API, so it's safe to discard here.
        client = FoundationClient(
            api_key=api_key or "",
            email=os.environ.get("FISERV_EMAIL", "user@fiserv.com"),
        )
    else:
        from openai import OpenAI
        # OpenAI SDK accepts arbitrary kwargs (timeout, max_retries, …);
        # forward everything we were given so callers can override.
        client = OpenAI(api_key=api_key, **kwargs)
    # Wrap so every chat/responses call records token usage centrally. This is
    # the single instrumentation point every agent funnels through.
    return _MeteredClient(client)


# ── Metered proxy — records token usage for every LLM round-trip ─────────────
#
# Wraps whatever make_client() would return (FoundationClient or openai.OpenAI)
# and intercepts `.chat.completions.create()` and `.responses.create()` so that
# after each call we hand the model + usage to run_metrics.record_call(). All
# attribute access that isn't chat/responses falls through to the real client,
# so the proxy is a transparent drop-in. Recording is wrapped in try/except and
# run_metrics is imported lazily, so instrumentation can never break an LLM call.

def _record_usage(requested_model, actual_model, usage) -> None:
    """Normalize a usage block (dict or SDK object, chat- or responses-shaped)
    and append it to the run_metrics tape. Captures BOTH the model name the
    caller requested AND the model the API actually used (response.model).

    On the Fiserv VDI the Foundation API is a proxy bound to a fixed endpoint:
    the model we *request* is just a hint (we set the X-Purpose header from
    it); the model that *actually serves* the request is whatever's deployed
    behind that purpose tag. Surfacing both lets the user see the discrepancy
    in the Run Details tab instead of trusting the request-side label.

    Never raises — metering can't break an LLM call."""
    try:
        if usage is None:
            it = ot = tt = 0
        else:
            def _g(obj, key):
                if isinstance(obj, dict):
                    return obj.get(key)
                return getattr(obj, key, None)
            # chat shape: prompt_tokens / completion_tokens
            # responses shape: input_tokens / output_tokens
            it = _g(usage, "prompt_tokens")
            if it is None:
                it = _g(usage, "input_tokens")
            ot = _g(usage, "completion_tokens")
            if ot is None:
                ot = _g(usage, "output_tokens")
            tt = _g(usage, "total_tokens")
        import run_metrics
        # Pass requested_model in the positional slot for back-compat with
        # callers that still send `record_call(model, in, out, total)`.
        run_metrics.record_call(
            requested_model, it, ot, tt, actual_model=actual_model,
        )
    except Exception:
        pass


def _extract_actual_model(resp, fallback: str) -> str:
    """Pull the actual model name from the API response object.

    Both the OpenAI SDK's ChatCompletion / Response objects AND the
    FoundationClient shim's _ChatCompletion / _ResponsesResponse expose
    ``.model``. If the proxy didn't echo a model name, ``.model`` is empty
    or None and we return "" (NOT the requested name) so the Run Details
    table can flag the missing echo explicitly instead of silently masking
    it as 'same as requested'."""
    val = getattr(resp, "model", None)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return ""


class _MeteredCompletions:
    def __init__(self, inner):
        self._inner = inner

    def create(self, *args, **kwargs):
        resp = self._inner.create(*args, **kwargs)
        requested = kwargs.get("model") or ""
        actual    = _extract_actual_model(resp, requested)
        _record_usage(requested, actual, getattr(resp, "usage", None))
        return resp

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _MeteredChat:
    def __init__(self, inner):
        self._inner = inner
        self.completions = _MeteredCompletions(inner.completions)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _MeteredResponses:
    def __init__(self, inner):
        self._inner = inner

    def create(self, *args, **kwargs):
        resp = self._inner.create(*args, **kwargs)
        requested = kwargs.get("model") or ""
        actual    = _extract_actual_model(resp, requested)
        _record_usage(requested, actual, getattr(resp, "usage", None))
        return resp

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _MeteredClient:
    """Transparent proxy over an OpenAI-shaped client that records token usage
    on every chat.completions.create / responses.create call."""
    def __init__(self, inner):
        self._inner = inner
        self.chat = _MeteredChat(inner.chat)
        # Some clients may not expose `.responses`; guard it.
        self.responses = (_MeteredResponses(inner.responses)
                          if getattr(inner, "responses", None) is not None
                          else None)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ── Response-header capture (rate-limit visibility) ──────────────────────────
# The Foundation API and OpenAI both return rate-limit info in HTTP response
# headers; there's no separate "describe my limits" endpoint. We cache the
# most recent set of relevant headers per (endpoint_url, purpose_tag) so the
# Streamlit UI can show what limits the proxy is enforcing right now — and the
# user can spot rate-limit warnings before they trigger an actual 429.

_HEADER_LOCK = __import__("threading").Lock()
_LATEST_HEADERS: dict = {}

# Header-name prefixes that indicate rate-limit / quota / throttle info.
# Lower-case match — httpx.Headers is case-insensitive on lookup but we iterate
# .items() in original case.
_INTERESTING_HEADER_PREFIXES = (
    "x-ratelimit",         # OpenAI native
    "x-ms-ratelimit",      # Azure OpenAI / Foundation API
    "retry-after",         # 429 hint
    "ratelimit-",          # IETF draft / some gateways
    "openai-",             # openai-processing-ms, openai-model, etc.
    "x-purpose",           # what purpose tag the gateway actually routed to
    "x-served-by",         # backend the proxy used
    "x-request-id",        # useful for support tickets
)


def _capture_response_headers(endpoint_url: str, purpose: str, headers) -> None:
    """Extract and stash the rate-limit-relevant headers from a successful
    Foundation API response. Keyed by (endpoint, purpose) so different
    purpose tags (GPT5.1Purpose vs GPT4.1Purpose) each show their own limits."""
    captured: dict = {}
    for k, v in headers.items():
        lk = k.lower()
        if any(lk.startswith(p) for p in _INTERESTING_HEADER_PREFIXES):
            captured[k] = v
    if not captured:
        # Nothing of interest — still record the snapshot timestamp so the UI
        # can show "responses observed but no rate-limit headers exposed".
        captured = {}
    key = f"{purpose or '?'}"          # Key on purpose; URL is usually constant
    import time as _t
    with _HEADER_LOCK:
        _LATEST_HEADERS[key] = {
            "endpoint": endpoint_url,
            "purpose":  purpose,
            "captured_at": _t.strftime("%Y-%m-%dT%H:%M:%S"),
            "headers":  captured,
        }


def get_latest_response_headers() -> dict:
    """Return a snapshot of the rate-limit headers captured on recent calls.

    Shape: ``{purpose_tag: {endpoint, purpose, captured_at, headers}}``.
    Returns an empty dict if no call has been made yet, or if the Foundation
    API is not in use (only the FoundationClient captures these — the real
    OpenAI SDK is used outside the VDI and its rate-limit headers live on
    `resp._raw_response.headers`, which we don't currently intercept)."""
    with _HEADER_LOCK:
        # Deep-ish copy so callers can mutate without racing the capturer.
        return {k: {**v, "headers": dict(v.get("headers", {}))}
                for k, v in _LATEST_HEADERS.items()}


# ── FoundationClient — looks like openai.OpenAI() ────────────────────────────

class FoundationClient:
    def __init__(self, api_key: str = "", email: str = "user@fiserv.com",
                 purpose: Optional[str] = None, url: Optional[str] = None,
                 purpose_gpt5: Optional[str] = None,
                 purpose_gpt4: Optional[str] = None):
        # api_key is accepted for API compatibility but unused inside the VDI —
        # Foundation API auth is handled by network egress + the custom headers.
        self.api_key = api_key
        self.email   = email

        # X-Purpose tags route the gateway to a model family (the deployment is
        # always "default").  GPT-5-class -> GPT5.1Purpose; else -> GPT4.1Purpose.
        self.purpose_gpt5 = purpose_gpt5 or os.environ.get(
            "FISERV_PURPOSE_GPT5", "GPT5.1Purpose")
        self.purpose_gpt4 = (
            purpose_gpt4
            or os.environ.get("FISERV_PURPOSE_GPT4")
            or os.environ.get("FISERV_PURPOSE")     # legacy single-purpose var
            or "GPT4.1Purpose"
        )
        # Fallback used only when a call doesn't pass a model name.
        self.purpose = purpose or self.purpose_gpt4

        self.url = url or os.environ.get(
            "FOUNDATION_API_URL",
            "https://dev-cst-cognitive-service.onefiserv.net"
            "/FoundationAPI/openai/deployments/default/chat/completions"
            "?api-version=2025-03-01-preview",
        )
        # Mimic the openai SDK namespaces.
        self.chat       = _ChatNamespace(self)
        self.responses  = _ResponsesNamespace(self)

    # ── model → X-Purpose mapping ─────────────────────────────────────────────
    def _purpose_for_model(self, model) -> str:
        """Pick the X-Purpose tag for the requested model.  GPT-5-family
        (e.g. 'gpt-5.1', 'gpt-5.2-2025-12-11') -> GPT5.1Purpose; all other
        models (gpt-4.1, gpt-4o-mini, …) -> GPT4.1Purpose."""
        if model and re.search(r"gpt-?5", str(model), re.I):
            return self.purpose_gpt5
        return self.purpose_gpt4

    # ── transport ────────────────────────────────────────────────────────────
    def _post(self, payload: dict, timeout: int = 120,
              purpose: Optional[str] = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "X-Purpose":    purpose or self.purpose,
            "X-Session-Id": str(uuid.uuid4()),
            "X-Source":     "PythonClient",
            "X-Email-Id":   self.email,
        }
        resp = httpx.post(self.url, json=payload, headers=headers, timeout=timeout)
        # Capture rate-limit-relevant response headers so the chatbot can
        # surface them in the UI. The Foundation API may forward Azure-style
        # x-ms-ratelimit-* OR OpenAI-style x-ratelimit-* headers; we grab any
        # that look interesting. This is the only way to learn the proxy's
        # rate limits without a separate admin endpoint.
        try:
            _capture_response_headers(self.url, purpose or self.purpose,
                                      resp.headers)
        except Exception:
            pass
        resp.raise_for_status()
        return resp.json()


# ── chat.completions.create() ────────────────────────────────────────────────

class _ChatNamespace:
    def __init__(self, parent: FoundationClient):
        self.completions = _ChatCompletions(parent)


class _ChatCompletions:
    """Forwards `create(messages=…)` to Foundation API and returns an object
    shaped like `openai.types.chat.ChatCompletion`."""
    def __init__(self, parent: FoundationClient):
        self.parent = parent

    def create(self, *, model: Optional[str] = None, messages: list,
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None,
               response_format: Optional[dict] = None,
               timeout: int = 120,
               **_ignored) -> "_ChatCompletion":
        payload: dict = {"messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            # Foundation API may or may not accept full json_schema — pass it
            # through and rely on server-side handling.
            payload["response_format"] = response_format
        raw = self.parent._post(payload, timeout=timeout,
                                purpose=self.parent._purpose_for_model(model))
        return _ChatCompletion(raw)


# ── responses.create() — translates Responses-API → chat/completions ─────────

class _ResponsesNamespace:
    """Shim for the Responses API.  Translates the `input=[...]` content
    blocks (`input_text`, `input_image`) into chat/completions content blocks
    (`text`, `image_url`) before posting."""
    def __init__(self, parent: FoundationClient):
        self.parent = parent

    def create(self, *, model: Optional[str] = None, input: list,
               text: Optional[dict] = None,
               max_output_tokens: Optional[int] = None,
               temperature: Optional[float] = None,
               timeout: int = 180,
               **_ignored) -> "_ResponsesResponse":
        messages = [self._translate_message(m) for m in input]
        payload: dict = {"messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens
        # If the caller asked for a strict json_schema, fall back to json mode —
        # callers that need schema validation parse + check on their side.
        if text and isinstance(text, dict):
            fmt = text.get("format", {})
            if fmt.get("type") in ("json_schema", "json_object"):
                payload["response_format"] = {"type": "json_object"}
        raw = self.parent._post(payload, timeout=timeout,
                                purpose=self.parent._purpose_for_model(model))
        return _ResponsesResponse(raw)

    # Image-detail level for vision calls. "high" preserves small-text /
    # checkbox legibility on contract PDFs (each image ~2,000 tokens vs ~85
    # for "low"). The previous hard-coded "low" was a major source of
    # silent extraction misses on the VDI/Fiserv backend — the colleague's
    # reference notebook uses the real OpenAI client which defaults to
    # "auto" (effectively "high" for legibility-critical images). Override
    # with the env var if you need to economise on tokens.
    _IMAGE_DETAIL = (os.environ.get("FISERV_IMAGE_DETAIL") or "high").lower()

    @staticmethod
    def _translate_message(msg: dict) -> dict:
        """Convert one Responses-API message into a chat/completions message."""
        role    = msg.get("role", "user")
        content = msg.get("content")
        if isinstance(content, list):
            new_content = []
            for item in content:
                t = item.get("type")
                if t == "input_text" or t == "text":
                    new_content.append({"type": "text", "text": item.get("text", "")})
                elif t == "input_image" or t == "image_url":
                    url = item.get("image_url")
                    if isinstance(url, dict):
                        url_val = url.get("url", "")
                    else:
                        url_val = url or ""
                    new_content.append({
                        "type": "image_url",
                        "image_url": {"url": url_val,
                                      "detail": _ResponsesNamespace._IMAGE_DETAIL},
                    })
                else:
                    # Unknown item type — pass through unchanged so the server
                    # can decide what to do.
                    new_content.append(item)
            content = new_content
        return {"role": role, "content": content}


# ── Response objects that look like the OpenAI SDK's ────────────────────────

class _ChatCompletion:
    """Shape: `.choices[0].message.content` — matches openai SDK."""
    def __init__(self, raw: dict):
        self.raw     = raw
        self.choices = [_Choice(c) for c in raw.get("choices", [])]
        # Surface usage if the gateway returns it.
        self.usage   = raw.get("usage", {})
        # Surface model if the gateway echoes it (used by metrics).
        self.model   = raw.get("model")


class _Choice:
    def __init__(self, raw: dict):
        self.message       = _Message(raw.get("message", {}))
        self.finish_reason = raw.get("finish_reason")
        self.index         = raw.get("index", 0)


class _Message:
    def __init__(self, raw: dict):
        self.content = raw.get("content", "") or ""
        self.role    = raw.get("role", "assistant")


class _ResponsesResponse:
    """Shape: `.output_text` — matches the OpenAI Responses API helper.
    The Foundation API responds in chat/completions shape, so we extract the
    first choice's message.content as `output_text`."""
    def __init__(self, raw: dict):
        self.raw  = raw
        choices   = raw.get("choices", [])
        self.output_text = (
            choices[0].get("message", {}).get("content", "") if choices else ""
        )
        # Also keep a Responses-API-like .output list for richer callers.
        self.output = [
            {"content": [{"type": "output_text", "text": self.output_text}]}
        ] if self.output_text else []
        self.usage = raw.get("usage", {})
        # Surface model if the gateway echoes it (used by metrics).
        self.model = raw.get("model")
