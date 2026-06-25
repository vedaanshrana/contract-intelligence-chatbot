"""
Shared UI building blocks for the Contract Intelligence Chatbot.

Centralizes the enterprise look-and-feel so chatbot.py stays focused on
behavior:

    from ui import (
        BRAND, inject_global_css, render_header,
        status_badge, status_pill, kpi_row, section_title,
    )

Nothing here calls the LLM or touches pipeline data — it's pure presentation.
"""
from __future__ import annotations

import html
from typing import Iterable, Optional

import streamlit as st


# ── Brand tokens ──────────────────────────────────────────────────────────────
# Single source of truth for colors so badges/cards/header stay consistent and
# match .streamlit/config.toml (primaryColor = NAVY).
class BRAND:
    NAVY        = "#154C7A"   # primary
    NAVY_DARK   = "#0E3658"
    ORANGE      = "#FF6200"   # Fiserv accent
    INK         = "#1B2733"   # body text
    MUTED       = "#5B6B7B"   # secondary text
    LINE        = "#E2E8F0"   # borders
    SURFACE     = "#FFFFFF"
    SURFACE_ALT = "#F2F5F8"

    # Semantic status colors
    GREEN  = "#1E8E5A"
    AMBER  = "#C97A0A"
    GREY   = "#8A97A6"
    RED    = "#C0392B"
    BLUE   = "#2563A8"


# Status vocabulary used across the app. (key -> (label, color, dot))
_STATUS = {
    "done":     ("Complete",   BRAND.GREEN, "●"),
    "partial":  ("In progress", BRAND.AMBER, "●"),
    "not_run":  ("Not run",    BRAND.GREY,  "○"),
    "ready":    ("Ready",      BRAND.BLUE,  "●"),
    "no_input": ("No input",   BRAND.GREY,  "○"),
    "error":    ("Error",      BRAND.RED,   "●"),
}


# ── Global CSS ──────────────────────────────────────────────────────────────--
def inject_global_css() -> None:
    """Inject the enterprise stylesheet. Call once, early, per page render."""
    st.markdown(
        f"""
        <style>
          /* ---- Layout density ---- */
          .block-container {{ padding-top: 0.6rem; padding-bottom: 2rem;
                              max-width: 1500px; }}
          header[data-testid="stHeader"] {{ background: transparent; }}
          .stAlert p {{ margin-bottom: 0; }}

          /* ---- Typography ---- */
          html, body, [class*="css"] {{
            font-family: "Segoe UI", "Inter", system-ui, -apple-system, sans-serif;
            color: {BRAND.INK};
          }}
          h1, h2, h3, h4 {{ color: {BRAND.NAVY_DARK}; letter-spacing: -0.01em; }}

          /* ---- Branded header bar ---- */
          .ci-header {{
            display: flex; align-items: center; gap: 0.85rem;
            padding: 0.65rem 1.05rem; margin: 0 0 0.4rem 0;
            background: linear-gradient(95deg, {BRAND.NAVY_DARK} 0%, {BRAND.NAVY} 60%);
            border-radius: 10px; color: #fff;
            box-shadow: 0 1px 3px rgba(16,40,64,0.18);
          }}
          .ci-header .ci-mark {{
            width: 34px; height: 34px; border-radius: 8px; flex: 0 0 auto;
            background: {BRAND.ORANGE}; color: #fff; font-weight: 700;
            display: flex; align-items: center; justify-content: center;
            font-size: 1.05rem;
          }}
          .ci-header .ci-title {{ font-size: 1.06rem; font-weight: 600; line-height: 1.15; }}
          .ci-header .ci-sub  {{ font-size: 0.76rem; opacity: 0.82; font-weight: 400; }}
          .ci-header .ci-crumb {{
            margin-left: auto; font-size: 0.82rem; opacity: 0.92;
            background: rgba(255,255,255,0.12); padding: 0.28rem 0.7rem;
            border-radius: 20px; white-space: nowrap;
          }}
          .ci-header .ci-crumb b {{ font-weight: 600; }}

          /* ---- Status badges / pills ---- */
          .ci-badge {{
            display: inline-flex; align-items: center; gap: 0.38rem;
            font-size: 0.74rem; font-weight: 600; padding: 0.13rem 0.55rem;
            border-radius: 20px; line-height: 1.4; white-space: nowrap;
          }}
          .ci-row {{
            display: flex; align-items: center; justify-content: space-between;
            gap: 0.6rem; padding: 0.34rem 0.1rem; border-bottom: 1px solid {BRAND.LINE};
          }}
          .ci-row:last-child {{ border-bottom: none; }}
          .ci-row .ci-name {{ font-weight: 600; font-size: 0.9rem; }}
          .ci-row .ci-meta {{ font-size: 0.76rem; color: {BRAND.MUTED}; }}

          /* ---- KPI cards ---- */
          .ci-kpis {{ display: flex; gap: 0.7rem; flex-wrap: wrap; margin: 0.2rem 0 0.6rem; }}
          .ci-kpi {{
            flex: 1 1 0; min-width: 130px;
            background: {BRAND.SURFACE}; border: 1px solid {BRAND.LINE};
            border-left: 3px solid {BRAND.NAVY};
            border-radius: 9px; padding: 0.7rem 0.85rem;
            box-shadow: 0 1px 2px rgba(16,40,64,0.05);
          }}
          .ci-kpi .ci-kpi-val {{ font-size: 1.5rem; font-weight: 700; color: {BRAND.NAVY_DARK};
                                 line-height: 1.05; }}
          .ci-kpi .ci-kpi-lbl {{ font-size: 0.72rem; color: {BRAND.MUTED};
                                 text-transform: uppercase; letter-spacing: 0.04em;
                                 margin-top: 0.18rem; }}
          .ci-kpi.accent {{ border-left-color: {BRAND.ORANGE}; }}
          .ci-kpi.warn   {{ border-left-color: {BRAND.AMBER}; }}

          /* ---- Tabs as segmented nav ---- */
          .stTabs [data-baseweb="tab-list"] {{ gap: 0.25rem; border-bottom: 1px solid {BRAND.LINE}; }}
          .stTabs [data-baseweb="tab"] {{
            font-weight: 600; font-size: 0.92rem; padding: 0.45rem 0.95rem;
            color: {BRAND.MUTED};
          }}
          .stTabs [aria-selected="true"] {{ color: {BRAND.NAVY}; }}

          /* ---- Buttons ---- */
          .stButton > button {{ border-radius: 8px; font-weight: 600; }}
          .stDownloadButton > button {{ border-radius: 8px; }}

          /* ---- Sidebar ---- */
          section[data-testid="stSidebar"] {{ background: {BRAND.SURFACE_ALT};
                                              border-right: 1px solid {BRAND.LINE}; }}

          /* ---- Section caption rule ---- */
          .ci-sec {{ font-size: 0.82rem; font-weight: 700; color: {BRAND.NAVY};
                     text-transform: uppercase; letter-spacing: 0.05em;
                     margin: 0.5rem 0 0.3rem; padding-bottom: 0.25rem;
                     border-bottom: 2px solid {BRAND.LINE}; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# ── Header bar ────────────────────────────────────────────────────────────────
def render_header(breadcrumb: str = "") -> None:
    """Render the branded top header. `breadcrumb` is rendered as raw HTML-safe text
    (callers may pass an already-escaped 'Core › Client' string via crumb())."""
    crumb_html = f'<div class="ci-crumb">{breadcrumb}</div>' if breadcrumb else ""
    st.markdown(
        f"""
        <div class="ci-header">
          <div class="ci-mark">CI</div>
          <div>
            <div class="ci-title">Contract Intelligence</div>
            <div class="ci-sub">Fiserv FI Billing &nbsp;·&nbsp; Kepler Cannon</div>
          </div>
          {crumb_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def crumb(*parts: str) -> str:
    """Build a safe ' Core › Client ' breadcrumb string for render_header()."""
    safe = [html.escape(str(p)) for p in parts if p not in (None, "")]
    return " &rsaquo; ".join(f"<b>{p}</b>" if i == len(safe) - 1 else p
                             for i, p in enumerate(safe))


# ── Status badge / pill ─────────────────────────────────────────────────────--
def status_badge(state: str) -> str:
    """Return inline HTML for a colored status pill. Use inside st.markdown(...,
    unsafe_allow_html=True)."""
    label, color, dot = _STATUS.get(state, _STATUS["not_run"])
    bg = _tint(color)
    return (f'<span class="ci-badge" style="background:{bg};color:{color}">'
            f'{dot} {html.escape(label)}</span>')


def status_pill(label: str, color: str) -> str:
    """Arbitrary colored pill (for custom labels like counts)."""
    return (f'<span class="ci-badge" style="background:{_tint(color)};color:{color}">'
            f'{html.escape(str(label))}</span>')


def client_row(name: str, state: str, meta: str = "") -> str:
    """One status row: name + meta on the left, badge on the right."""
    meta_html = f'<span class="ci-meta">{html.escape(meta)}</span>' if meta else ""
    return (f'<div class="ci-row"><div><span class="ci-name">{html.escape(name)}</span> '
            f'{meta_html}</div>{status_badge(state)}</div>')


# ── KPI cards ─────────────────────────────────────────────────────────────────
def kpi_row(cards: Iterable[tuple]) -> None:
    """Render a row of KPI cards. Each card is (value, label[, variant]).
    variant ∈ {"", "accent", "warn"}."""
    items = []
    for card in cards:
        value, label = card[0], card[1]
        variant = card[2] if len(card) > 2 else ""
        cls = f"ci-kpi {variant}".strip()
        items.append(
            f'<div class="{cls}"><div class="ci-kpi-val">{html.escape(str(value))}</div>'
            f'<div class="ci-kpi-lbl">{html.escape(str(label))}</div></div>'
        )
    st.markdown(f'<div class="ci-kpis">{"".join(items)}</div>', unsafe_allow_html=True)


def section_title(text: str) -> None:
    st.markdown(f'<div class="ci-sec">{html.escape(text)}</div>', unsafe_allow_html=True)


# ── helpers ─────────────────────────────────────────────────────────────────--
def _tint(hex_color: str, alpha: float = 0.14) -> str:
    """Return an rgba() tint of a #rrggbb color for badge backgrounds."""
    h = hex_color.lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        r, g, b = 90, 100, 110
    return f"rgba({r},{g},{b},{alpha})"
