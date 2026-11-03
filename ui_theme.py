"""Apple-style visual theme for UltraLogistics Pro.

Same look as the EAS Estimating Tool: light grey ground, white grouped cards,
logo ink (#231f20) for text and logo amber (#e4a323) as the single accent.
SF Pro on Apple devices, Inter elsewhere.

Nothing in here touches the logistics logic. Usage in the main script:

    import ui_theme as ui
    ui.apply_theme()                       # once per run, before anything is drawn
    ui.page_header("UltraLogistics Pro", "Crating & load planning")
    fig = ui.style_fig(fig)                # for Plotly figures

Optional logo: put it at assets/eas_logo.png (or eas_logo.png) next to the script.
"""
from __future__ import annotations

import base64
import html as _html
import os
from typing import Optional

import streamlit as st

try:
    _VERSION = tuple(int(p) for p in st.__version__.split(".")[:2])
except Exception:  # noqa: BLE001
    _VERSION = (0, 0)

# Streamlit 1.50+ replaces use_container_width=True with width="stretch".
# Usage: st.button("Go", **ui.STRETCH)
STRETCH = {"width": "stretch"} if _VERSION >= (1, 50) else {"use_container_width": True}

INK = "#231f20"
AMBER = "#e4a323"

_LOGO_CANDIDATES = [
    "assets/eas_logo.png",
    "assets/logo.png",
    "eas_logo.png",
    "logo.png",
]

_CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
:root{
--u-ink:#231f20;--u-ink-2:#6e6e73;--u-ink-3:#8e8e93;
--u-bg:#f5f5f7;--u-surface:#ffffff;--u-line:rgba(0,0,0,.1);--u-line-soft:rgba(0,0,0,.06);
--u-amber:#e4a323;--u-amber-strong:#b57a00;--u-amber-text:#8a5a00;--u-amber-soft:rgba(228,163,35,.16);
--u-font:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display","Inter","Segoe UI",system-ui,sans-serif;
--u-radius:12px;
}
.stApp{background:var(--u-bg);color:var(--u-ink)}
.stApp,.stApp p,.stApp label,.stApp li,.stApp input,.stApp textarea,.stApp button,
.stApp h1,.stApp h2,.stApp h3,.stApp h4,.stApp table,.stApp [data-baseweb="select"],
.stApp [data-baseweb="tab"],.stApp [data-testid="stMetricValue"]{font-family:var(--u-font) !important}
.stApp [data-testid="stIconMaterial"],.stApp .material-icons{font-family:"Material Symbols Rounded","Material Icons" !important}

header[data-testid="stHeader"]{background:transparent}
.stAppDeployButton,[data-testid="stDecoration"],footer{display:none !important}
.block-container{max-width:1280px;padding:1.2rem 1.6rem 5rem}
h1,h2,h3,h4{letter-spacing:-.02em;color:var(--u-ink)}
h1{font-weight:700}
h2,h3{font-weight:650}
.stApp [data-testid="stCaptionContainer"],.stApp small{color:var(--u-ink-2)}
hr{border-color:var(--u-line-soft) !important}
a{color:var(--u-amber-text)}

/* ---- sidebar: grouped list on a slightly darker ground ---- */
[data-testid="stSidebar"]{background:#ececf0;border-right:1px solid var(--u-line-soft)}
[data-testid="stSidebar"] .block-container,[data-testid="stSidebarContent"]{padding-top:1rem}
[data-testid="stSidebar"] h1{font-size:1.35rem}

/* ---- tabs: floating segmented control ---- */
[data-baseweb="tab-list"]{position:sticky;top:3.2rem;z-index:60;gap:2px;padding:3px;width:fit-content;max-width:100%;
margin:0 auto 1.5rem;background:rgba(229,229,234,.88);-webkit-backdrop-filter:saturate(180%) blur(20px);
backdrop-filter:saturate(180%) blur(20px);border-radius:11px;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow-x:auto}
[data-baseweb="tab-highlight"],[data-baseweb="tab-border"]{display:none !important}
button[data-baseweb="tab"]{height:34px;padding:0 16px;border-radius:9px;background:transparent;color:var(--u-ink);font-weight:500;font-size:.92rem}
button[data-baseweb="tab"] p{font-size:.92rem;font-weight:500;color:inherit}
button[data-baseweb="tab"][aria-selected="true"]{background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.14),0 0 0 .5px rgba(0,0,0,.04);font-weight:600}
[data-baseweb="tab-panel"]{padding-top:.25rem}

/* ---- buttons ---- */
.stButton>button,.stDownloadButton>button,[data-testid="stFormSubmitButton"]>button{
border-radius:10px;border:1px solid var(--u-line);background:#fff;color:var(--u-ink);font-weight:500;
box-shadow:0 1px 1px rgba(0,0,0,.04);transition:background .15s,transform .05s}
.stButton>button:hover,.stDownloadButton>button:hover,[data-testid="stFormSubmitButton"]>button:hover{
border-color:var(--u-amber);color:var(--u-ink);background:#fffaf0}
.stButton>button:active,.stDownloadButton>button:active{transform:scale(.985)}
.stButton>button[kind="primary"],[data-testid="stFormSubmitButton"]>button[kind="primary"],
.stButton>button[data-testid="stBaseButton-primary"]{background:var(--u-amber);border-color:var(--u-amber);color:var(--u-ink);font-weight:600}
.stButton>button[kind="primary"]:hover,.stButton>button[data-testid="stBaseButton-primary"]:hover{background:#d39512;border-color:#d39512;color:var(--u-ink)}
.stButton>button:disabled{opacity:.45}
.stButton>button:focus-visible,.stDownloadButton>button:focus-visible{outline:3px solid var(--u-amber-soft);outline-offset:1px}

/* ---- inputs ---- */
[data-baseweb="input"],[data-baseweb="base-input"],[data-baseweb="textarea"],[data-baseweb="select"]>div{
border-radius:10px !important;background:#fff !important;border-color:var(--u-line) !important}
[data-baseweb="input"]:focus-within,[data-baseweb="textarea"]:focus-within,[data-baseweb="select"]>div:focus-within{
border-color:var(--u-amber) !important;box-shadow:0 0 0 3px var(--u-amber-soft) !important}
[data-baseweb="tag"]{background:var(--u-amber-soft) !important;color:var(--u-amber-text) !important;border-radius:7px !important}
[data-testid="stFileUploaderDropzone"]{background:#fff;border:1px dashed rgba(0,0,0,.2);border-radius:var(--u-radius)}
[data-testid="stCheckbox"] [data-baseweb="checkbox"] span:first-child{border-radius:6px}
[data-testid="stSlider"] [role="slider"]{background:var(--u-amber);box-shadow:0 0 0 4px var(--u-amber-soft)}

/* ---- cards ---- */
[data-testid="stMetric"]{background:#fff;border:1px solid var(--u-line-soft);border-radius:14px;padding:14px 16px;
box-shadow:0 1px 2px rgba(0,0,0,.04)}
[data-testid="stMetricLabel"] p{color:var(--u-ink-2);font-size:.78rem;font-weight:500;text-transform:none}
[data-testid="stMetricValue"]{font-weight:650;letter-spacing:-.02em;font-size:1.5rem;color:var(--u-ink)}
[data-testid="stExpander"]{background:#fff;border:1px solid var(--u-line-soft) !important;border-radius:var(--u-radius);
box-shadow:0 1px 2px rgba(0,0,0,.04);overflow:hidden}
[data-testid="stExpander"] summary{font-weight:550}
[data-testid="stForm"]{background:#fff;border:1px solid var(--u-line-soft);border-radius:16px;padding:1.2rem;
box-shadow:0 2px 12px rgba(0,0,0,.05)}
[data-testid="stDataFrame"],[data-testid="stDataEditor"]{border:1px solid var(--u-line-soft);border-radius:var(--u-radius);
overflow:hidden;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.04)}
[data-testid="stPlotlyChart"]{background:#fff;border:1px solid var(--u-line-soft);border-radius:14px;padding:6px;
box-shadow:0 1px 2px rgba(0,0,0,.04)}


/* ---- pills and headlines ---- */
.u-pill{display:inline-flex;align-items:center;height:26px;padding:0 10px;border-radius:13px;font-size:13px;font-weight:600;white-space:nowrap}
.u-pill-good{background:rgba(52,199,89,.18);color:#1b6f31}
.u-pill-warn{background:rgba(255,159,10,.18);color:#8a5000}
.u-pill-bad{background:rgba(255,59,48,.14);color:#b3001a}
.u-pill-neutral{background:rgba(118,118,128,.14);color:#3a3a3c}
.u-h1{font-size:2rem;font-weight:700;letter-spacing:-.03em;line-height:1.15;margin:.2rem 0 0;color:var(--u-ink)}
.u-lead{font-size:1rem;color:var(--u-ink-2);margin:.35rem 0 1rem;max-width:46rem;line-height:1.5}
.stApp [data-testid="stVerticalBlockBorderWrapper"]{border-radius:14px}
[data-testid="stSegmentedControl"]{margin-bottom:1rem}

/* ---- alerts: soft tinted pills ---- */
[data-testid="stAlert"]{border-radius:var(--u-radius);border:none}
[data-testid="stAlert"][data-baseweb="notification"]{border-radius:var(--u-radius)}

/* ---- brand header ---- */
.u-head{display:flex;align-items:center;gap:14px;margin:.2rem 0 1rem}
.u-head img{height:38px;width:auto;display:block}
.u-mark{width:38px;height:38px;border-radius:10px;background:var(--u-ink);color:var(--u-amber);display:grid;place-items:center;
font-weight:700;font-size:1.05rem;letter-spacing:-.02em}
.u-title{font-size:1.65rem;font-weight:700;letter-spacing:-.03em;line-height:1.1;color:var(--u-ink)}
.u-sub{font-size:.86rem;color:var(--u-ink-2);margin-top:2px}
.u-login{max-width:380px;margin:8vh auto 1rem;text-align:center}
.u-login .u-head{justify-content:center;flex-direction:column;gap:10px}
.u-login .u-title{font-size:1.5rem}

@media (max-width:720px){
.block-container{padding:.8rem .8rem 4rem}
[data-baseweb="tab-list"]{top:3rem;width:100%}
button[data-baseweb="tab"]{padding:0 10px}
}
@media (prefers-color-scheme:dark){
/* keep it light on purpose: logistics sheets are read like paper */
}
"""


def _find_logo() -> Optional[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in _LOGO_CANDIDATES:
        path = os.path.join(here, rel)
        if os.path.isfile(path):
            return path
    return None


def _logo_html() -> str:
    path = _find_logo()
    if path:
        try:
            with open(path, "rb") as fh:
                data = base64.b64encode(fh.read()).decode("ascii")
            mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
            return f'<img alt="EAS" src="data:{mime};base64,{data}">'
        except OSError:
            pass
    return '<div class="u-mark">EAS</div>'


def apply_theme() -> None:
    """Inject the stylesheet. Call once per script run, before drawing anything."""
    st.markdown(f"<style>{_CSS}</style>", unsafe_allow_html=True)


def page_header(title: str, subtitle: str = "") -> None:
    sub = f'<div class="u-sub">{_html.escape(subtitle)}</div>' if subtitle else ""
    st.markdown(
        f'<div class="u-head">{_logo_html()}<div><div class="u-title">{_html.escape(title)}</div>{sub}</div></div>',
        unsafe_allow_html=True,
    )


def login_header(title: str, subtitle: str = "") -> None:
    sub = f'<div class="u-sub">{_html.escape(subtitle)}</div>' if subtitle else ""
    st.markdown(
        f'<div class="u-login"><div class="u-head">{_logo_html()}'
        f'<div><div class="u-title">{_html.escape(title)}</div>{sub}</div></div></div>',
        unsafe_allow_html=True,
    )


def style_fig(fig, height: Optional[int] = None):
    """Apply the Apple look to a Plotly figure (fonts, transparent ground, soft grid)."""
    fig.update_layout(
        font=dict(family='-apple-system, BlinkMacSystemFont, "SF Pro Text", Inter, "Segoe UI", sans-serif',
                  color=INK, size=12),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        title=dict(font=dict(size=15, color=INK)),
        hoverlabel=dict(bgcolor="white", font=dict(color=INK, size=12), bordercolor="rgba(0,0,0,.1)"),
    )
    soft = dict(gridcolor="rgba(0,0,0,.06)", zerolinecolor="rgba(0,0,0,.1)", linecolor="rgba(0,0,0,.15)")
    try:
        fig.update_xaxes(**soft)
        fig.update_yaxes(**soft)
    except Exception:  # noqa: BLE001  3D figures use scene axes
        pass
    if height:
        fig.update_layout(height=height)
    return fig


def pill(text: str, tone: str = "neutral") -> str:
    """Small status pill as HTML (use with unsafe_allow_html=True)."""
    tone = tone if tone in ("good", "warn", "bad", "neutral") else "neutral"
    return f'<span class="u-pill u-pill-{tone}">{_html.escape(str(text))}</span>'


def headline(title: str, subtitle: str = "", pill_html: str = "") -> None:
    """Large step heading with an optional lead sentence and status pill above it."""
    lead = f'<div class="u-lead">{_html.escape(subtitle)}</div>' if subtitle else ""
    top = f'<div style="margin-bottom:.4rem">{pill_html}</div>' if pill_html else ""
    st.markdown(f'{top}<h1 class="u-h1">{_html.escape(title)}</h1>{lead}', unsafe_allow_html=True)
