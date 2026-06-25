"""
Contract Intelligence — HTTP API (FastAPI).

Replaces the Streamlit UI. Wraps the existing agent pipeline, chat engine, and
context builder behind a small REST + Server-Sent-Events API that the React
front-end (../frontend) consumes. Also serves the built React SPA in production.

Run (dev):   python server.py            # or: uvicorn server:app --reload
Run (prod):  python server.py            # serves ../frontend/dist if it exists

The OpenAI / Fiserv backend selection, API keys, models, and folder layout are
all inherited from config.py exactly as the old app used them — nothing about
the underlying engine changes.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import app_settings
import agent_runner as ar

app = FastAPI(title="Contract Intelligence API", version="1.0.0")

# Dev: the Vite dev server runs on :5173 and calls this API on :8000. In prod
# the SPA is served from this same origin so CORS is moot — allowing the dev
# origins is harmless. Allow all for an internal tool to avoid setup friction.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

SPA_DIST = (Path(__file__).parent.parent / "frontend" / "dist").resolve()


# ── Request models ──────────────────────────────────────────────────────────--

class ChatRequest(BaseModel):
    focus: list[str]
    messages: list[dict]
    snowflake: bool = True


class SettingsPatch(BaseModel):
    chat_model: Optional[str] = None
    hier_model: Optional[str] = None
    extr_model: Optional[str] = None
    match_model: Optional[str] = None
    cpi_model: Optional[str] = None
    engagement_model: Optional[str] = None
    scope_model: Optional[str] = None
    dict_path: Optional[str] = None
    min_year: Optional[int] = None


class RunRequest(BaseModel):
    client: str
    core: str = ""


class FeedbackRequest(BaseModel):
    category: str
    title: str
    description: str = ""
    question: str = ""
    answer: str = ""
    core: str = ""
    clients: list[str] = []
    chat_model: str = ""
    user_name: str = ""
    user_email: str = ""


# ── SSE helper ──────────────────────────────────────────────────────────────--

async def _sse(gen_factory):
    """Run a *blocking* sync generator (which does agent work) on a worker
    thread and stream its yielded dict events as SSE without blocking the event
    loop."""
    q: queue.Queue = queue.Queue()
    SENTINEL = object()

    def worker():
        try:
            for ev in gen_factory():
                q.put(ev)
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(SENTINEL)

    threading.Thread(target=worker, daemon=True).start()
    loop = asyncio.get_event_loop()

    async def stream():
        # Initial comment primes some proxies and the EventSource connection.
        yield ": stream-open\n\n"
        while True:
            ev = await loop.run_in_executor(None, q.get)
            if ev is SENTINEL:
                break
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# ── Meta / config ─────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/config")
def get_config():
    return {
        "backend": config.OPENAI_BACKEND,
        "settings": app_settings.get_all(),
        "models": [
            config.CHAT_MODEL, config.HIERARCHY_MODEL, config.EXTRACTION_MODEL,
            config.MATCHING_MODEL, config.CPI_MODEL, config.MASTER_CONTRACT_MODEL,
            config.SCOPE_AGENT_MODEL, "gpt-4o-mini", "gpt-4.1-2025-04-14",
            "gpt-5.2", "gpt-5.2-2025-12-11",
        ],
        "agents": [{"key": k, "display": d} for k, d in ar.FRONTEND_AGENTS],
    }


@app.get("/api/settings")
def get_settings():
    return app_settings.get_all()


@app.put("/api/settings")
def put_settings(patch: SettingsPatch):
    return app_settings.update({k: v for k, v in patch.dict().items() if v is not None})


# ── Cores & clients ─────────────────────────────────────────────────────────--

@app.get("/api/cores")
def get_cores():
    cores = ar.list_cores()
    return [{"name": c, "clients": len(ar.list_clients(c))} for c in cores]


@app.get("/api/clients")
def get_clients(core: str = "", status: bool = True):
    clients = ar.list_clients(core)
    if not status:
        return [{"client": c} for c in clients]
    return [ar.client_status(c) for c in clients]


@app.get("/api/clients/{client}/status")
def get_client_status(client: str):
    return ar.client_status(client)


@app.get("/api/clients/{client}/metrics")
def get_client_metrics(client: str):
    return ar.client_metrics(client)


@app.get("/api/portfolio")
def get_portfolio(clients: str = ""):
    names = [c for c in clients.split(",") if c.strip()]
    return ar.portfolio_kpis(names)


# ── PDFs ────────────────────────────────────────────────────────────────────--

@app.get("/api/clients/{client}/pdfs")
def get_client_pdfs(client: str, core: str = ""):
    if not core:
        core = ar._core_for_client(client)
    return {"core": core, "pdfs": ar.list_pdfs(client, core)}


@app.get("/api/clients/{client}/pdfs/{name}")
def get_pdf(client: str, name: str, core: str = ""):
    if not core:
        core = ar._core_for_client(client)
    p = ar.resolve_pdf_path(client, core, name)
    if not p:
        raise HTTPException(404, f"PDF not found: {name}")
    return FileResponse(str(p), media_type="application/pdf", filename=name,
                        headers={"Content-Disposition": f'inline; filename="{name}"'})


# ── Agent outputs ─────────────────────────────────────────────────────────────

@app.get("/api/clients/{client}/outputs/{key}")
def get_output(client: str, key: str):
    return ar.read_output(key, client)


@app.get("/api/clients/{client}/outputs/{key}/export")
def export_output(client: str, key: str):
    p = ar.output_path_for(key, client)
    if not p or not Path(p).exists():
        raise HTTPException(404, "Output not found")
    safe = client.replace(" ", "_")
    fname = f"{safe}_{key}.xlsx"
    return FileResponse(
        str(p),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=fname,
    )


# ── Interactive hierarchy graph (Plotly HTML) ─────────────────────────────────
# Palette mirrors frontend/src/index.css so the embedded Plotly graph blends
# into the React app's light/dark theme instead of rendering on a white card.
_PLOTLY_THEMES = {
    "dark":  {"bg": "#09090b", "surface": "#18181b", "line": "#27272a",
              "ink": "#fafafa", "ink2": "#a1a1aa"},
    "light": {"bg": "#f9fafb", "surface": "#ffffff", "line": "#e5e7eb",
              "ink": "#111827", "ink2": "#4b5563"},
}
_PLOTLY_FONT = "'Inter', ui-sans-serif, system-ui, -apple-system, sans-serif"


def _theme_plotly_html(html: str, theme: str) -> str:
    """Inject CSS + a post-load Plotly.relayout so the standalone graph adopts
    the app's aesthetic (fonts, transparent canvas, themed axes/legend/menus).
    Semantic data colours — confidence buckets, era bands — are left intact.

    Non-destructive: only the served copy is transformed. The file on disk is
    untouched, so 'Open in new tab' / download still yield the original."""
    t = _PLOTLY_THEMES.get(theme, _PLOTLY_THEMES["dark"])
    style = f"""<style>
html,body{{margin:0;padding:0;background:{t['bg']};}}
body{{font-family:{_PLOTLY_FONT};}}
.plotly-graph-div{{margin:0 auto;}}
::-webkit-scrollbar{{width:8px;height:8px;}}
::-webkit-scrollbar-thumb{{background:{t['line']};border-radius:4px;}}
::-webkit-scrollbar-track{{background:transparent;}}
</style>"""
    ink, ink2 = json.dumps(t["ink"]), json.dumps(t["ink2"])
    line, surf = json.dumps(t["line"]), json.dumps(t["surface"])
    fontj = json.dumps(_PLOTLY_FONT)
    script = f"""<script>(function(){{
var INK={ink},INK2={ink2},LINE={line},SURF={surf},FONT={fontj};
function apply(){{
var gd=document.querySelector('.plotly-graph-div');
if(!gd||!window.Plotly||!gd.layout){{return setTimeout(apply,60);}}
var r={{'paper_bgcolor':'rgba(0,0,0,0)','plot_bgcolor':'rgba(0,0,0,0)',
'font.color':INK,'font.family':FONT,'title.font.color':INK,
'legend.font.color':INK2,'legend.bgcolor':'rgba(0,0,0,0)',
'xaxis.color':INK2,'yaxis.color':INK2,
'xaxis.gridcolor':LINE,'yaxis.gridcolor':LINE,
'xaxis.linecolor':LINE,'yaxis.linecolor':LINE,
'xaxis.zerolinecolor':LINE,'yaxis.zerolinecolor':LINE}};
var ms=(gd.layout.updatemenus)||[];
for(var i=0;i<ms.length;i++){{
r['updatemenus['+i+'].bgcolor']=SURF;
r['updatemenus['+i+'].bordercolor']=LINE;
r['updatemenus['+i+'].font.color']=INK;}}
try{{window.Plotly.relayout(gd,r);}}catch(e){{}}
}}
if(document.readyState!=='loading'){{apply();}}
else{{document.addEventListener('DOMContentLoaded',apply);}}
}})();</script>"""

    html = html.replace("</head>", style + "</head>", 1) if "</head>" in html else style + html
    html = html.replace("</body>", script + "</body>", 1) if "</body>" in html else html + script
    return html


@app.get("/api/clients/{client}/hierarchy/html")
def get_hierarchy_html(client: str, theme: str = "dark"):
    """Serve the Hierarchy agent's interactive Plotly graph, re-themed to match
    the app. Fully interactive (zoom/pan/hover/dropdowns) — the JS is embedded."""
    p = ar.hierarchy_html_path(client)
    if not p:
        raise HTTPException(
            404, "Interactive hierarchy graph not found — run the Hierarchy agent first.")
    try:
        html = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Could not read hierarchy graph: {e}")
    return HTMLResponse(_theme_plotly_html(html, theme))


# ── Running agents (SSE) ────────────────────────────────────────────────────--

@app.post("/api/clients/{client}/load")
async def load_client(client: str, core: str = "", force: bool = False):
    if not core:
        core = ar._core_for_client(client)
    return await _sse(lambda: ar.pipeline_events(client, core, force=force))


@app.post("/api/agents/{key}/run")
async def run_agent(key: str, req: RunRequest):
    core = req.core or ar._core_for_client(req.client)

    def gen():
        yield {"type": "agent_start", "key": key, "display": key,
               "index": 0, "total": 1}
        logs: list[str] = []
        result = ar.run_one_agent(key, req.client, core, log=lambda m: logs.append(str(m)))
        for line in logs[-30:]:
            yield {"type": "log", "key": key, "message": line}
        status = (result or {}).get("status", "complete")
        ok = status in ("complete", "ok", "cached") or (result and "rows" in result and status != "error")
        yield {"type": "agent_done", "key": key, "display": key,
               "status": "complete" if ok else (status or "error"),
               "summary": ar._summarize_result(key, result),
               "index": 0, "total": 1, "agentsDone": 1 if ok else 0,
               "result": result}
        yield {"type": "pipeline_done", "client": req.client,
               "agentsDone": 1 if ok else 0, "total": 1, "elapsedMs": 0}

    return await _sse(gen)


# ── Chat ────────────────────────────────────────────────────────────────────--

@app.post("/api/chat")
def chat(req: ChatRequest):
    if not req.focus:
        raise HTTPException(400, "focus must include at least one client")
    try:
        return ar.build_chat_reply(req.focus, req.messages, use_snowflake=req.snowflake)
    except Exception as e:  # noqa: BLE001
        # Surface the error as a normal assistant reply so the UI can show it
        # instead of a generic 500 page.
        return {"reply": f"⚠️ Chat error: {e}\n\nCheck the model name in Settings.",
                "citations": [], "invoices": []}


# ── Feedback (chatbot answer reports) ──────────────────────────────────────────

@app.post("/api/feedback")
def submit_feedback(req: FeedbackRequest):
    if not req.title.strip() and not req.description.strip():
        raise HTTPException(400, "Provide a title or description")
    try:
        return ar.save_feedback(req.dict())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Could not save feedback: {e}")


# ── Snowflake invoice diagnostics ──────────────────────────────────────────────

@app.get("/api/snowflake/status")
def snowflake_status():
    try:
        import snowflake_invoice as _sf
        if not _sf.snowflake_available():
            return {"available": False,
                    "message": "snowflake-connector-python is not installed."}
        return {"available": True, "status": _sf.connection_status()}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "message": str(e)}


# ── Dictionary (per Core) ──────────────────────────────────────────────────────

@app.get("/api/cores/{core}/dictionary")
def get_dictionary(core: str):
    override = app_settings.get("dict_path")
    resolved = config.default_dictionary_for(core)
    return {
        "core": core,
        "override": override or "",
        "resolved": str(resolved) if resolved and Path(resolved).exists() else "",
        "resolvedName": (Path(resolved).name
                         if resolved and Path(resolved).exists() else ""),
        "matchingEnabled": bool(override or (resolved and Path(resolved).exists())),
    }


@app.post("/api/cores/{core}/dictionary")
async def upload_dictionary(core: str, file: UploadFile = File(...)):
    core_dir = config.INPUT_DIR / core
    if not core_dir.exists():
        raise HTTPException(404, f"Core not found: {core}")
    name = Path(file.filename or "dictionary.xlsx").name
    if not name.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Dictionary must be an .xlsx/.xls file")
    dest = core_dir / name
    data = await file.read()
    dest.write_bytes(data)
    # Point the active dictionary override at the uploaded file.
    app_settings.update({"dict_path": str(dest)})
    return {"saved": str(dest), "name": name}


# ── Static SPA (production) ─────────────────────────────────────────────────--
# Mounted LAST so /api/* routes always win. html=True serves index.html for any
# unmatched path (SPA deep-linking).
if SPA_DIST.exists():
    app.mount("/", StaticFiles(directory=str(SPA_DIST), html=True), name="spa")
else:
    @app.get("/")
    def _no_spa():
        return JSONResponse(
            {"message": "API is running. Build the React app (cd '../frontend' && "
                        "npm install && npm run build) to serve the UI here, or "
                        "run the Vite dev server on :5173."},
        )


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run("server:app", host=host, port=port, reload=False)
