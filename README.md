# MASTER_CHATBOT

Unified Contract Intelligence package combining:

| Layer | Source | Notes |
| --- | --- | --- |
| **Backend agents** (latest) | `Contract Chatbot - Demo` | Hierarchy, Engagement Overview, Product Module, Fee Description, Material Match, **Material Validation** (Snowflake), CPI, Termination, plus backend-only Scope Triage / Term & Renewal / SLA / Volume Tiers |
| **MNR Template Agent** | `Contract Chatbot - SNOWFLAKE` | Stage-1 forensic extraction + Stage-2 matching against the Frequently Used Material Codes catalog, producing a SAP-ready MNR Excel draft |
| **FastAPI server** + persistent settings | `PATCHED_UI/Contract Chatbot - Streamlit` | `server.py`, `agent_runner.py`, `app_settings.py` — wraps the agents behind REST + SSE for the React UI |
| **React UI** | `PATCHED_UI/UI 2` | Vite + React, dev-proxies `/api` -> `:8000`, builds into `frontend/dist` for prod single-origin serve |

Every other module (config, chat_engine, context_builder, run_metrics, snowflake_invoice, the ui/chatbot Streamlit pages, all other agents) comes from **Demo (latest)**. The MNR additions to `config.py` (MNR_*) and the MNR registration in `chatbot.py` + `agent_runner.py` are the only merge points.

## Layout

```
MASTER_CHATBOT/
├── backend/                        # Python — agents, chat engine, FastAPI
│   ├── server.py                   # FastAPI entrypoint (REST + SSE)
│   ├── agent_runner.py             # UI-free orchestration layer
│   ├── app_settings.py             # persisted model/dictionary settings
│   ├── chatbot.py                  # legacy Streamlit UI (still works)
│   ├── chat_engine.py
│   ├── context_builder.py
│   ├── config.py                   # incl. MNR_* keys
│   ├── run_metrics.py
│   ├── snowflake_invoice.py
│   ├── aggregated_retrieval.py     # MNR retrieval helper
│   ├── agents/
│   │   ├── hierarchy.py
│   │   ├── engagement_overview.py
│   │   ├── product_module.py
│   │   ├── scope_agent.py
│   │   ├── extraction.py
│   │   ├── material_match.py
│   │   ├── material_validation.py  # (Snowflake reranker)
│   │   ├── mnr_template.py         # (new MNR agent)
│   │   ├── cpi.py
│   │   ├── term_renewal.py
│   │   ├── termination.py
│   │   ├── sla.py
│   │   ├── volume_tiers.py
│   │   ├── dna_extraction.py
│   │   ├── master_contract.py
│   │   ├── clause_extractor.py
│   │   ├── pdf_annotator.py
│   │   └── _text_utils.py
│   ├── Input/                      # PDFs (drop Input/<Core>/<Client>/*.pdf)
│   ├── Output/                     # Per-client agent outputs
│   ├── marked_checkbox_example.png # used by MNR
│   ├── snowflake_config.example.toml
│   └── requirements.txt
├── frontend/                       # React (Vite + TS)
│   ├── src/
│   │   ├── App.tsx
│   │   ├── api.ts                  # talks to /api/*
│   │   ├── constants.ts            # 9-agent list (incl. material_validation + mnr_template)
│   │   ├── types.ts                # AgentKey union covers all 9
│   │   ├── store.tsx
│   │   ├── components/             # agents, chatbot, dashboard, layout, ui
│   │   ├── main.tsx
│   │   └── index.css
│   ├── index.html
│   ├── package.json
│   └── vite.config.ts
├── Launch (Production).bat         # builds React, runs FastAPI single-origin
├── Launch (Dev).bat                # FastAPI :8000 + Vite :5173 in two windows
└── Launch (Streamlit Legacy).bat   # old Streamlit UI, no React
```

## Frontend agents (9, in pipeline order)

1. Hierarchy Agent
2. Engagement Overview Agent
3. Product Module Agent
4. Fee Description Agent
5. Material Code Matching Agent
6. **Material Validation Agent** *(needs Snowflake / Fiserv VDI; self-skips when unreachable)*
7. CPI Terms Agent
8. Termination Clause Agent
9. **MNR Template Agent** *(needs `Frequently Used Material Codes.xlsx` + a master agreement)*

Backend-only agents (Scope Triage, Term & Renewal, SLA, Volume Tiers) still run during Load and still feed the chat context, but are not shown as cards.

## First-time setup

```powershell
# 1. Python deps (backend)
cd backend
pip install -r requirements.txt

# 2. Frontend deps
cd ..\frontend
npm install
```

Drop contracts under `backend/Input/<Core>/<Client>/*.pdf`. Drop the per-Core
material dictionary anywhere in `backend/Input/<Core>/` (the largest `.xlsx`
wins, or any filename containing "dict"). The MNR agent additionally looks for
a `Frequently Used Material Codes.xlsx` in the Core folder or `backend/`.

For the Material Validation agent, copy
`backend/snowflake_config.example.toml` to `backend/snowflake_config.toml` and
fill in your VDI Snowflake credentials. If the file is missing or unreachable,
that agent self-skips (Material Code Matching remains system of record).

## Running

| Goal | Command |
| --- | --- |
| Production (single origin) | `Launch (Production).bat` -> open http://127.0.0.1:8000 |
| Dev with hot reload (recommended while editing UI) | `Launch (Dev).bat` -> open http://127.0.0.1:5173 |
| Legacy Streamlit (no React) | `Launch (Streamlit Legacy).bat` |

### Manual equivalents

```powershell
# Backend (FastAPI)
cd backend
python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000

# Frontend (Vite dev) — separate terminal
cd frontend
npm run dev
```

For production, build the React app once (`cd frontend && npm run build`) — the
FastAPI server will pick up `frontend/dist/index.html` and serve the SPA from
the same origin as the API.

## API surface

All routes live under `/api/*`. See `backend/server.py` for the full list; the
React client in `frontend/src/api.ts` documents the shapes. The key endpoints
the new agents bring in:

- `GET  /api/clients/{client}/outputs/material_validation` -> validated rows
- `GET  /api/clients/{client}/outputs/mnr_template` -> MNR draft rows
- `POST /api/agents/material_validation/run` -> single-agent run (SSE)
- `POST /api/agents/mnr_template/run` -> single-agent run (SSE)
- `POST /api/clients/{client}/load` -> full pipeline (SSE, runs all 9 + internals)

## Environment

The API key fallback chain is the same as the source projects — set
`EXTRACTION_API_KEY` (or `OPENAI_API_KEY`) and the rest cascade. Per-agent
override variables remain available:

- `VALIDATION_API_KEY`, `VALIDATION_MODEL`
- `MNR_API_KEY`, `MNR_MODEL`
- `HIERARCHY_API_KEY`, `MASTER_CONTRACT_API_KEY`, `CPI_API_KEY`, `SCOPE_AGENT_API_KEY`

Set `OPENAI_BACKEND=fiserv` to route everything through the Fiserv VDI
Foundation API instead of OpenAI.
