"""
Contract Helper — the simple, biller-friendly front end.

A stripped-down companion to chatbot.py for non-technical users (billers,
accountants). Three zones: pick a client (left), read answers (center),
ask in plain English (right). No agents, departments, or settings on screen.

Reuses the existing engine: config, context_builder, chat_engine.
Run:  streamlit run simple_app.py   (opens at http://localhost:8501)
"""
from pathlib import Path

import streamlit as st

import config
from context_builder import build_multi_client_context
from chat_engine import ChatEngine

# ── Page setup ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Contract Helper", page_icon="📄", layout="wide")

st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 1rem; }
      /* bigger, calmer type for non-technical users */
      html, body, [class*="css"] { font-size: 17px; }
      .ch-title { font-size: 1.6rem; font-weight: 700; margin-bottom: .1rem; }
      .ch-sub   { color: #6b7280; margin-bottom: 1rem; }
      .ch-zone-label { font-size: .8rem; text-transform: uppercase; letter-spacing: .05em;
                       color: #6b7280; font-weight: 700; margin-bottom: .4rem; }
      div[data-testid="stVerticalBlock"] div.stButton > button {
          width: 100%; text-align: left; padding: .7rem .9rem; border-radius: 12px;
          font-weight: 600; margin-bottom: .35rem;
      }
      .client-pill { padding:.2rem 0; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Helpers (folder scan — same logic the full app uses) ─────────────────────
DEFAULT_CORE = "PORTICO"


def list_cores() -> list[str]:
    if not config.INPUT_DIR.exists():
        return []
    return sorted(p.name for p in config.INPUT_DIR.iterdir() if p.is_dir())


def list_clients(core: str) -> list[str]:
    base = config.INPUT_DIR / core if core else config.INPUT_DIR
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def count_pdfs(client: str, core: str) -> int:
    d = config.client_input_dir(client, core)
    return len(list(d.glob("*.pdf"))) if d.exists() else 0


@st.cache_resource(show_spinner=False)
def get_engine() -> ChatEngine:
    return ChatEngine(config.CHAT_API_KEY, config.CHAT_MODEL)


def get_context(client: str) -> dict:
    """Build (and cache per client) the contract context for the chat."""
    cache = st.session_state.setdefault("_ctx_cache", {})
    if client not in cache:
        cache[client] = build_multi_client_context([client])
    return cache[client]


def ask(client: str, question: str) -> None:
    """Send a question through the engine and append to the transcript."""
    history = st.session_state.setdefault("chat", {}).setdefault(client, [])
    history.append({"role": "user", "content": question})
    ctx = get_context(client)
    engine = get_engine()
    system_prompt = engine.build_system_prompt(
        client_name=client,
        kb_context=ctx.get("kb", ""),
        hierarchy_context=ctx.get("hierarchy", ""),
        extraction_context=ctx.get("extraction", ""),
        cpi_context=ctx.get("cpi", ""),
        clauses_context=ctx.get("clauses", ""),
        master_contract_context=ctx.get("master_contract", ""),
    )
    try:
        with st.spinner("Reading the contracts…"):
            reply = engine.chat(history, system_prompt)
    except Exception as exc:  # keep the UI calm for end users
        reply = f"Sorry — I couldn't get an answer just now. ({exc})"
    history.append({"role": "assistant", "content": reply})


# Common-question buttons → plain-English prompts the assistant can always answer.
QUICK_ACTIONS = [
    ("💰  Check Pricing",
     "Give me a clear summary of all fees and prices in this client's contracts, "
     "and point out anything that looks unusual or inconsistent."),
    ("🔎  Find Missing Charges",
     "List any services or fees that might not be getting billed — for example items "
     "marked Included, Waived, or with no clear charge — and flag anything that could be missed revenue."),
    ("📄  New Contract Setup Info",
     "List everything I'd need to set this contract up in SAP: every billable line item with "
     "its price and, where available, its matched material code."),
    ("📈  Check CPI Increases",
     "List every CPI or annual escalation term across this client's contracts: the percentage or index, "
     "any floor or cap, and when the next increase is due."),
]

# ── Session defaults ─────────────────────────────────────────────────────────
st.session_state.setdefault("core", DEFAULT_CORE)
st.session_state.setdefault("client", None)
st.session_state.setdefault("pending_q", None)

# ── Header ───────────────────────────────────────────────────────────────────
st.markdown('<div class="ch-title">📄 Contract Helper</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="ch-sub">Pick a client, then ask anything about their contracts — in plain English.</div>',
    unsafe_allow_html=True,
)

left, center, right = st.columns([1.1, 2.3, 1.9], gap="large")

# ── LEFT — pick a client + add a contract ────────────────────────────────────
with left:
    st.markdown('<div class="ch-zone-label">1 · Pick a client</div>', unsafe_allow_html=True)

    cores = list_cores()
    if len(cores) > 1:
        st.session_state.core = st.selectbox(
            "Product", cores,
            index=cores.index(st.session_state.core) if st.session_state.core in cores else 0,
            label_visibility="collapsed",
        )
    core = st.session_state.core

    clients = list_clients(core)
    if not clients:
        st.info("No clients found yet. Add a contract below to get started.")
    for c in clients:
        n = count_pdfs(c, core)
        label = f"{'✅' if c == st.session_state.client else '📁'}  {c}  ·  {n} file{'s' if n != 1 else ''}"
        if st.button(label, key=f"cli_{c}"):
            st.session_state.client = c

    st.markdown("&nbsp;", unsafe_allow_html=True)
    st.markdown('<div class="ch-zone-label">Add a contract</div>', unsafe_allow_html=True)
    up = st.file_uploader(
        "Upload PDF(s)", type=["pdf"], accept_multiple_files=True,
        label_visibility="collapsed",
    )
    if up:
        target_client = st.session_state.client or "NEW CLIENT"
        dest = config.client_input_dir(target_client, core)
        dest.mkdir(parents=True, exist_ok=True)
        saved = 0
        for f in up:
            (dest / f.name).write_bytes(f.getbuffer())
            saved += 1
        st.success(f"Saved {saved} file(s) to “{target_client}”.")

# ── CENTER — the contract + answers ──────────────────────────────────────────
with center:
    st.markdown('<div class="ch-zone-label">2 · Answers</div>', unsafe_allow_html=True)
    client = st.session_state.client
    if not client:
        st.info("👈 Pick a client on the left to begin.")
    else:
        st.subheader(client)
        st.caption(f"{count_pdfs(client, core)} contract file(s) on record")
        history = st.session_state.get("chat", {}).get(client, [])
        if not history:
            st.write("Ask a question on the right, or tap one of the quick buttons. "
                     "Your answers will appear here.")
        for msg in history:
            with st.chat_message("user" if msg["role"] == "user" else "assistant"):
                st.markdown(msg["content"])

# ── RIGHT — ask anything + quick buttons ─────────────────────────────────────
with right:
    st.markdown('<div class="ch-zone-label">3 · Ask</div>', unsafe_allow_html=True)
    client = st.session_state.client
    disabled = client is None

    st.caption("Common questions")
    for label, prompt in QUICK_ACTIONS:
        if st.button(label, key=f"qa_{label}", disabled=disabled):
            st.session_state.pending_q = prompt

    st.markdown("&nbsp;", unsafe_allow_html=True)
    with st.form("ask_form", clear_on_submit=True):
        typed = st.text_area(
            "Your question", placeholder="e.g. What's the auto-renew notice period?",
            label_visibility="collapsed", height=90, disabled=disabled,
        )
        sent = st.form_submit_button("Ask", disabled=disabled, type="primary")
        if sent and typed.strip():
            st.session_state.pending_q = typed.strip()

# ── Run a pending question, then refresh so it shows in the center ───────────
if st.session_state.pending_q and st.session_state.client:
    q = st.session_state.pending_q
    st.session_state.pending_q = None
    ask(st.session_state.client, q)
    st.rerun()
