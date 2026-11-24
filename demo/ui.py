"""IronAsk operator console.

    streamlit run ui.py

Design note. The interface sits between two things the user already knows: the
machine control panel and the procedure sheet. So the app chrome is dark
brushed steel and the conversation sits on cool document stock. Two conventions
are borrowed from the plant's own documents rather than invented: status is an
annunciator lamp (MAN-FUR-02 4.3 defines green / amber / red lamps for the
interlock chain), and a citation is an engineering title block carrying document
number, revision, effective date and sheet. Identifiers are set in a mono face
because E-417 against E-471, and 0 against O, are real hazards here.

The sidebar lists only what a user CAN open. Naming what they cannot would
disclose that a restricted document exists.
"""
from __future__ import annotations

import html
import os

import httpx
import pandas as pd
import streamlit as st

API = os.getenv("API_BASE", "http://localhost:8080")

st.set_page_config(
    page_title="IronAsk - Ironvale Components",
    page_icon="◰",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# tokens + styling
# ---------------------------------------------------------------------------
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;450;600&display=swap');

:root{
  --steel-900:#1B2227;      /* panel body */
  --steel-800:#232C33;
  --steel-700:#2E3942;      /* raised panel */
  --steel-500:#55646F;
  --steel-300:#8C9AA5;      /* muted legend text */
  --paper:#E9EAE7;          /* document stock */
  --paper-2:#DEDFDB;        /* ruled / inset paper */
  --ink:#171C1F;
  --ink-soft:#4A545B;
  --rule:#C3C6C0;
  --lamp-green:#3F9A5C;
  --lamp-amber:#C8902A;
  --lamp-red:#B4432F;
  --signal:#2F6F8F;         /* control-panel legend blue */
  --sans:'IBM Plex Sans',system-ui,sans-serif;
  --cond:'Barlow Condensed',Impact,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,monospace;
}

/* Streamlit's own chrome: out of the way, we build our own bar. */
[data-testid="stHeader"]{display:none;}
[data-testid="stToolbar"]{display:none;}
#MainMenu, footer{visibility:hidden;}

.stApp{background:var(--paper);}
html, body, [class*="st-"]{font-family:var(--sans);color:var(--ink);}
.block-container{padding:0 3rem 7rem 3rem; max-width:1180px;}

/* ----------------------------------------------------------- top bar ---- */
.topbar{
  display:flex; align-items:center; gap:1.1rem;
  margin:0 -3rem 1.9rem -3rem; padding:.85rem 3rem;
  background:var(--steel-900);
  border-bottom:3px solid var(--signal);
}
.mark{
  width:30px;height:30px;flex:0 0 30px;
  border:2px solid var(--paper); border-radius:2px;
  position:relative; background:var(--steel-700);
}
/* quartered circle: the machine-status glyph on the DMC panel */
.mark:before{
  content:""; position:absolute; inset:5px;
  border-radius:50%; border:2px solid var(--paper);
  border-right-color:transparent; border-bottom-color:transparent;
}
.wordmark{
  font-family:var(--cond); font-weight:700; font-size:1.42rem;
  letter-spacing:.055em; color:var(--paper); line-height:1;
}
.plant{
  font-family:var(--cond); font-weight:500; font-size:.97rem;
  color:var(--steel-300); letter-spacing:.03em; line-height:1;
  padding-left:1.1rem; border-left:1px solid var(--steel-500);
}
.topbar .spacer{flex:1;}
.statline{display:flex; gap:1.4rem; align-items:center;}
.stat{
  font-family:var(--mono); font-size:.73rem; color:var(--steel-300);
  display:flex; align-items:center; gap:.42rem;
}

/* ------------------------------------------------- annunciator lamp ---- */
.lamp{
  width:9px;height:9px;border-radius:50%;display:inline-block;flex:0 0 9px;
  background:var(--steel-500);
}
.lamp-green{background:var(--lamp-green); box-shadow:0 0 0 2px rgba(63,154,92,.22);}
.lamp-amber{background:var(--lamp-amber); box-shadow:0 0 0 2px rgba(200,144,42,.22);}
.lamp-red{background:var(--lamp-red);   box-shadow:0 0 0 2px rgba(180,67,47,.22);}
.lamp-grey{background:var(--steel-500); box-shadow:0 0 0 2px rgba(85,100,111,.18);}

/* ------------------------------------------------------- readout ------- */
.readout{
  display:flex; align-items:center; gap:.7rem; flex-wrap:wrap;
  padding:.4rem 0 .55rem 0; border-bottom:1px solid var(--rule);
  margin-bottom:.95rem;
}
.readout .state{
  font-family:var(--cond); font-weight:600; font-size:1.02rem;
  letter-spacing:.045em; color:var(--ink);
}
.readout .meta{
  font-family:var(--mono); font-size:.72rem; color:var(--ink-soft);
  padding-left:.75rem; border-left:1px solid var(--rule);
}
.answer{
  font-size:1.12rem; line-height:1.62; max-width:66ch;
  margin:0 0 1.15rem 0; color:var(--ink);
}
.answer sup{
  font-family:var(--mono); font-size:.62em; color:var(--signal);
  font-weight:600; padding-left:.08em;
}
.rewritten{
  font-size:.86rem; color:var(--ink-soft); margin:0 0 .85rem 0;
  padding:.45rem .7rem; background:var(--paper-2);
  border-left:2px solid var(--steel-500); max-width:66ch;
}
.rewritten b{font-family:var(--mono); font-size:.92em; font-weight:500; color:var(--ink);}

/* ------------------------------------------------------ title block ---- */
.tb-head{
  font-family:var(--cond); font-weight:600; font-size:.88rem;
  letter-spacing:.05em; color:var(--ink-soft); margin:1.3rem 0 .45rem 0;
}
.titleblock{
  display:grid; grid-template-columns:2.1rem 1fr auto;
  border:1px solid var(--steel-500); background:#F2F3F0;
  margin-bottom:.4rem;
}
.tb-n{
  display:flex; align-items:center; justify-content:center;
  background:var(--steel-700); color:var(--paper);
  font-family:var(--mono); font-size:.78rem; font-weight:600;
}
.tb-body{padding:.5rem .75rem; min-width:0;}
.tb-doc{
  font-family:var(--mono); font-size:.84rem; font-weight:600; color:var(--ink);
}
.tb-title{font-size:.86rem; color:var(--ink-soft); margin-top:.1rem;}
.tb-cells{display:flex; border-left:1px solid var(--rule);}
.tb-cell{
  padding:.42rem .7rem; min-width:5.4rem;
  border-right:1px solid var(--rule);
}
.tb-cell:last-child{border-right:none;}
.tb-k{
  font-family:var(--cond); font-size:.7rem; letter-spacing:.05em;
  color:var(--steel-500); line-height:1.2;
}
.tb-v{font-family:var(--mono); font-size:.8rem; color:var(--ink); line-height:1.35;}
.tb-super{color:var(--lamp-amber); font-weight:600;}

/* ------------------------------------------------------- graph path ---- */
.chain{
  display:flex; align-items:stretch; flex-wrap:wrap; gap:0;
  margin:.3rem 0 .5rem 0;
}
.node{
  background:#F2F3F0; border:1px solid var(--steel-500);
  padding:.3rem .6rem; min-width:0;
}
.node .nl{
  font-family:var(--cond); font-size:.66rem; letter-spacing:.055em;
  color:var(--steel-500); line-height:1.15; display:block;
}
.node .nn{
  font-family:var(--mono); font-size:.79rem; color:var(--ink);
  line-height:1.3; display:block;
}
.rel{
  display:flex; align-items:center; padding:0 .42rem;
  font-family:var(--cond); font-size:.68rem; letter-spacing:.06em;
  color:var(--signal); white-space:nowrap;
}
.rel:before{
  content:""; width:.42rem; height:1px; background:var(--steel-500);
  margin-right:.35rem;
}
.rel:after{
  content:""; width:.42rem; height:1px; background:var(--steel-500);
  margin-left:.35rem;
}

/* --------------------------------------------------------- no result --- */
.noresult{
  border:1px solid var(--rule); border-left:3px solid var(--lamp-amber);
  background:var(--paper-2); padding:.9rem 1.05rem; max-width:66ch;
}
.noresult p{margin:0; font-size:1rem; color:var(--ink);}
.noresult .hint{
  font-size:.84rem; color:var(--ink-soft); margin-top:.45rem;
}

/* ------------------------------------------------------- data block ---- */
.metric-note{
  font-family:var(--mono); font-size:.73rem; color:var(--ink-soft);
  border-top:1px solid var(--rule); padding-top:.4rem; margin-top:.3rem;
}

/* ------------------------------------------------------------ sidebar -- */
[data-testid="stSidebar"]{background:var(--steel-900); border-right:none;}
[data-testid="stSidebar"] *{color:var(--paper);}
[data-testid="stSidebar"] .block-container{padding-top:1rem;}
.badge{
  background:var(--steel-700); border:1px solid var(--steel-500);
  padding:.75rem .85rem; margin-bottom:1rem;
}
.badge .who{
  font-family:var(--cond); font-weight:700; font-size:1.22rem;
  letter-spacing:.02em; line-height:1.1; color:var(--paper);
}
.badge .role{
  font-size:.83rem; color:var(--steel-300); margin-top:.15rem;
}
.badge .scope{
  font-family:var(--mono); font-size:.74rem; color:var(--paper);
  margin-top:.5rem; padding-top:.5rem; border-top:1px solid var(--steel-500);
}
.legend{
  font-family:var(--cond); font-size:.78rem; letter-spacing:.055em;
  color:var(--steel-300); margin:1.15rem 0 .4rem 0;
}
.opens{list-style:none; padding:0; margin:0;}
.opens li{
  font-size:.84rem; color:var(--paper); padding:.17rem 0 .17rem .85rem;
  position:relative;
}
.opens li:before{
  content:""; position:absolute; left:0; top:.62rem;
  width:4px; height:4px; background:var(--lamp-green);
}
.note{font-size:.76rem; color:var(--steel-300); line-height:1.5; margin-top:.5rem;}

/* sidebar buttons = question chips */
[data-testid="stSidebar"] .stButton>button{
  width:100%; text-align:left; white-space:normal; height:auto;
  background:transparent; color:var(--paper);
  border:1px solid var(--steel-500); border-radius:2px;
  font-family:var(--sans); font-size:.82rem; line-height:1.35;
  padding:.44rem .6rem; margin-bottom:.3rem;
}
[data-testid="stSidebar"] .stButton>button:hover{
  border-color:var(--signal); background:var(--steel-800); color:#fff;
}
[data-testid="stSidebar"] .stButton>button:focus-visible{
  outline:2px solid var(--signal); outline-offset:1px;
}

/* ------------------------------------------------------------- chat ---- */
[data-testid="stChatMessage"]{background:transparent; padding:.25rem 0;}
[data-testid="stChatMessageAvatarUser"],
[data-testid="stChatMessageAvatarAssistant"]{display:none;}
.askline{
  font-family:var(--cond); font-weight:600; font-size:1.3rem;
  letter-spacing:.01em; color:var(--ink); max-width:66ch;
  padding:.1rem 0 .1rem .85rem; border-left:3px solid var(--steel-700);
  margin:1.6rem 0 1rem 0;
}
[data-testid="stChatInput"] textarea{font-size:1rem;}
[data-testid="stChatInput"]{border-top:1px solid var(--rule);}

/* ---------------------------------------------------------- expander --- */
[data-testid="stExpander"]{border:none; background:transparent;}
[data-testid="stExpander"] summary{
  font-family:var(--cond); font-size:.82rem; letter-spacing:.045em;
  color:var(--signal); padding:.1rem 0;
}
[data-testid="stExpander"] summary:hover{color:var(--ink);}

/* ------------------------------------------------------ empty state --- */
.hero{max-width:62ch; padding:1.6rem 0 .4rem 0;}
.hero h1{
  font-family:var(--cond); font-weight:700; font-size:2.5rem;
  line-height:1.06; letter-spacing:.005em; margin:0 0 .6rem 0; color:var(--ink);
}
.hero p{font-size:1.02rem; line-height:1.6; color:var(--ink-soft); margin:0;}
.cols{display:grid; grid-template-columns:repeat(3,1fr); gap:1.4rem; margin-top:2.1rem;}
.col h3{
  font-family:var(--cond); font-weight:600; font-size:.92rem;
  letter-spacing:.05em; color:var(--ink); margin:0 0 .4rem 0;
  padding-bottom:.3rem; border-bottom:1px solid var(--rule);
}
.col p{font-size:.86rem; color:var(--ink-soft); line-height:1.5; margin:0;}

@media (max-width:860px){
  .block-container{padding-left:1.2rem; padding-right:1.2rem;}
  .topbar{margin-left:-1.2rem; margin-right:-1.2rem; padding-left:1.2rem; padding-right:1.2rem;}
  .cols{grid-template-columns:1fr;}
  .plant{display:none;}
}
@media (prefers-reduced-motion:reduce){
  *{animation:none !important; transition:none !important;}
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
STATE = {
    "answered":     ("lamp-green", "Answered"),
    "not_found":    ("lamp-amber", "No result"),
    "out_of_scope": ("lamp-grey",  "Out of scope"),
    "error":        ("lamp-red",   "Error"),
}

E = html.escape


@st.cache_data(ttl=30, show_spinner=False)
def load_users():
    return httpx.get(f"{API}/v1/users", timeout=10).json()


@st.cache_data(ttl=30, show_spinner=False)
def load_scope(user_id: str):
    """Fetched per selected user, so one browser never learns another user's
    access. See /v1/scope."""
    try:
        return httpx.get(f"{API}/v1/scope", params={"user_id": user_id},
                         timeout=10).json()
    except Exception:
        return {}


@st.cache_data(ttl=30, show_spinner=False)
def load_health():
    try:
        return httpx.get(f"{API}/health", timeout=5).json()
    except Exception:
        return {}


def mark_citations(answer: str) -> str:
    """Render [1] markers as superscripts without trusting the text as HTML."""
    out = E(answer)
    for n in range(1, 10):
        out = out.replace(f"[{n}]", f"<sup>{n}</sup>")
    return out


def render_titleblock(c: dict) -> str:
    cells = []
    if c.get("version"):
        cells.append(("Rev", E(str(c["version"]))))
    if c.get("effective_date"):
        cells.append(("Effective", E(str(c["effective_date"]))))
    if c.get("page") is not None:
        cells.append(("Sheet", f"p.{E(str(c['page']))}"))
    if c.get("source") == "scanned":
        cells.append(("Origin", "scan"))
    if c.get("superseded"):
        cells.append(("Status", "<span class='tb-super'>superseded</span>"))

    cell_html = "".join(
        f"<div class='tb-cell'><div class='tb-k'>{k}</div>"
        f"<div class='tb-v'>{v}</div></div>"
        for k, v in cells
    )
    return (
        f"<div class='titleblock'>"
        f"<div class='tb-n'>{c['n']}</div>"
        f"<div class='tb-body'>"
        f"<div class='tb-doc'>{E(c['doc_id'])}</div>"
        f"<div class='tb-title'>{E(c['title'])}"
        + (f" &mdash; {E(str(c['section']))}" if c.get("section") else "")
        + f"</div></div>"
        f"<div class='tb-cells'>{cell_html}</div>"
        f"</div>"
    )


def render_chain(path: list[str]) -> str:
    parts = []
    for item in path:
        if ":" in item:
            label, name = item.split(":", 1)
            parts.append(
                f"<div class='node'><span class='nl'>{E(label)}</span>"
                f"<span class='nn'>{E(name)}</span></div>"
            )
        else:
            parts.append(f"<div class='rel'>{E(item.replace('_', ' ').lower())}</div>")
    return f"<div class='chain'>{''.join(parts)}</div>"


def render_answer(data: dict) -> None:
    status = data.get("status", "error")
    lamp, label = STATE.get(status, STATE["error"])
    ms = (data.get("pipeline") or {}).get("timings_ms", {}).get("total")

    meta = [f"{data.get('route') or '-'} route"]
    if ms is not None:
        meta.append(f"{ms:.0f} ms")
    top = (data.get("pipeline") or {}).get("top_score")
    if top is not None:
        meta.append(f"score {top}")

    st.markdown(
        f"<div class='readout'><span class='lamp {lamp}'></span>"
        f"<span class='state'>{label}</span>"
        + "".join(f"<span class='meta'>{E(m)}</span>" for m in meta)
        + "</div>",
        unsafe_allow_html=True,
    )

    rq = data.get("rewritten_query")
    if rq and rq.strip().lower() != (data.get("question") or "").strip().lower():
        st.markdown(
            f"<div class='rewritten'>Read as <b>{E(rq)}</b></div>",
            unsafe_allow_html=True,
        )

    answer = data.get("answer", "")

    if status in ("not_found", "out_of_scope"):
        hint = (
            "Try naming the document, machine or part directly, for example "
            "<b>SOP-L3-007</b> or <b>CNC-L1-04</b>."
            if status == "not_found" else
            "IronAsk covers procedures, equipment manuals, production data and "
            "supplier relationships for this plant."
        )
        st.markdown(
            f"<div class='noresult'><p>{E(answer)}</p>"
            f"<div class='hint'>{hint}</div></div>",
            unsafe_allow_html=True,
        )
        return

    st.markdown(f"<div class='answer'>{mark_citations(answer)}</div>",
                unsafe_allow_html=True)

    # ---- citations as title blocks
    cits = data.get("citations") or []
    if cits:
        st.markdown(
            f"<div class='tb-head'>Source{'s' if len(cits) > 1 else ''}</div>",
            unsafe_allow_html=True,
        )
        for c in cits:
            st.markdown(render_titleblock(c), unsafe_allow_html=True)
            if c.get("snippet"):
                with st.expander(f"Read {c['doc_id']} as filed"):
                    st.markdown(
                        f"<div style='font-size:.9rem;line-height:1.6;"
                        f"color:var(--ink-soft);max-width:70ch'>"
                        f"{E(c['snippet'])}</div>",
                        unsafe_allow_html=True,
                    )

    # ---- SQL + chart
    sql = data.get("sql")
    if sql:
        st.markdown("<div class='tb-head'>Query</div>", unsafe_allow_html=True)
        with st.expander("Show the SQL that produced this"):
            st.code(sql["query"], language="sql")
        if sql.get("rows"):
            df = pd.DataFrame(sql["rows"], columns=sql["columns"])
            ch = sql.get("chart") or {}
            if ch.get("type") in ("bar", "line") and ch.get("x") and ch.get("y"):
                plot = df.set_index(ch["x"])[ch["y"]]
                (st.bar_chart if ch["type"] == "bar" else st.line_chart)(
                    plot, color="#2F6F8F", height=260
                )
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.markdown(
                f"<div class='metric-note'>{sql.get('row_count', 0)} row(s) "
                f"returned</div>", unsafe_allow_html=True,
            )

    # ---- graph
    graph = data.get("graph")
    if graph and graph.get("paths"):
        params = ", ".join(f"{k}={v}" for k, v in (graph.get("params") or {}).items())
        st.markdown(
            f"<div class='tb-head'>Relationships &mdash; "
            f"{E(graph.get('template') or '')}({E(params)})</div>",
            unsafe_allow_html=True,
        )
        for path in graph["paths"][:8]:
            st.markdown(render_chain(path), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------
try:
    users = load_users()
except Exception:
    users = []

health = load_health()

st.markdown(
    "<div class='topbar'>"
    "<div class='mark'></div>"
    "<div><div class='wordmark'>IRONASK</div></div>"
    "<div class='plant'>Ironvale Components<br>Hosur plant</div>"
    "<div class='spacer'></div>"
    "<div class='statline'>"
    f"<span class='stat'><span class='lamp "
    f"{'lamp-green' if health.get('ok') else 'lamp-red'}'></span>"
    f"{'service up' if health.get('ok') else 'service down'}</span>"
    f"<span class='stat'>{health.get('chunks', 0)} documents indexed</span>"
    "<span class='stat'>line 1 &middot; line 2 &middot; line 3</span>"
    "</div></div>",
    unsafe_allow_html=True,
)

if not users:
    st.markdown(
        "<div class='noresult'><p>No connection to the IronAsk service.</p>"
        "<div class='hint'>Start it with "
        "<b>uvicorn app:app --port 8080</b>, then reload this page.</div></div>",
        unsafe_allow_html=True,
    )
    st.stop()

# ---------------------------------------------------------------------------
# sidebar: shift badge, what this user can open, starters
# ---------------------------------------------------------------------------
STARTERS = [
    ("Procedures", [
        "What does alarm E-417 mean on the DMC 850?",
        "What is the soak time for HX-220-B?",
        "What torque for M8 flange bolts?",
    ]),
    ("Production data", [
        "Breakdown downtime by machine on Line 1 in October 2026",
        "What is the yield on Line 3 in October 2026?",
    ]),
    ("Supplier impact", [
        "Which suppliers are behind deviations on Line 1 this year?",
        "If Arcflow is put on hold, which machines and SOPs are affected?",
    ]),
]

with st.sidebar:
    names = {f"{u['name']} — {u['role'].replace('_', ' ')}": u for u in users}
    picked = st.selectbox("Signed in as", list(names), label_visibility="collapsed")
    user = names[picked]

    st.markdown(
        f"<div class='badge'>"
        f"<div class='who'>{E(user['name'])}</div>"
        f"<div class='role'>{E(user['role'].replace('_', ' ').title())}</div>"
        f"<div class='scope'>{E(user['scope'])}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    opens = (load_scope(user["user_id"]) or {}).get("can_open") or []
    if opens:
        st.markdown("<div class='legend'>You can open</div>", unsafe_allow_html=True)
        st.markdown(
            "<ul class='opens'>"
            + "".join(f"<li>{E(o)}</li>" for o in opens)
            + "</ul>",
            unsafe_allow_html=True,
        )
    st.markdown(
        "<div class='note'>Your role and line come from the plant's user "
        "record. Anything a question claims about who you are is ignored.</div>",
        unsafe_allow_html=True,
    )

    st.markdown("<div class='legend'>Ask about</div>", unsafe_allow_html=True)
    for group, qs in STARTERS:
        st.caption(group)
        for q in qs:
            if st.button(q, key=f"s_{abs(hash(q))}"):
                st.session_state["pending"] = q

    st.markdown("<div class='legend'>Session</div>", unsafe_allow_html=True)
    if st.button("Clear conversation", key="clear"):
        st.session_state["messages"] = []
        st.rerun()

# Changing user starts a new thread: history must not cross identities.
if st.session_state.get("_uid") != user["user_id"]:
    st.session_state["_uid"] = user["user_id"]
    st.session_state["messages"] = []

messages = st.session_state.setdefault("messages", [])

# ---------------------------------------------------------------------------
# conversation
# ---------------------------------------------------------------------------
if not messages:
    st.markdown(
        "<div class='hero'>"
        "<h1>The approved procedure, and where you are losing time.</h1>"
        "<p>Ask about an alarm, a torque figure, a soak time, last month's "
        "downtime, or which supplier sits behind a deviation. Every answer "
        "carries the document it came from, down to the printed page.</p>"
        "</div>"
        "<div class='cols'>"
        "<div class='col'><h3>Procedures and manuals</h3>"
        "<p>Answers quote the controlling document and its revision, and say "
        "when a figure comes from a superseded sheet.</p></div>"
        "<div class='col'><h3>Production data</h3>"
        "<p>Downtime, yield and defect rates come from the plant database, with "
        "the SQL and the metric definition shown.</p></div>"
        "<div class='col'><h3>Supplier impact</h3>"
        "<p>Trace a deviation back to a supplier, or see which machines and "
        "procedures a supplier hold would touch.</p></div>"
        "</div>",
        unsafe_allow_html=True,
    )

for m in messages:
    if m["role"] == "user":
        st.markdown(f"<div class='askline'>{E(m['text'])}</div>",
                    unsafe_allow_html=True)
    else:
        render_answer(m["payload"])

pending = st.session_state.pop("pending", None)
question = st.chat_input("Ask about a procedure, a machine or a supplier") or pending

if question:
    history = [
        {"role": m["role"], "content": m["text"]} for m in messages
    ][-6:]
    messages.append({"role": "user", "text": question})
    st.markdown(f"<div class='askline'>{E(question)}</div>", unsafe_allow_html=True)

    with st.spinner("Checking the documents and data you can open"):
        try:
            data = httpx.post(
                f"{API}/v1/ask",
                json={"user_id": user["user_id"], "question": question,
                      "history": history},
                timeout=90,
            ).json()
        except Exception:
            data = {
                "status": "error", "route": None,
                "answer": "Something went wrong. Try again.",
                "question": question, "citations": [], "pipeline": {},
            }

    render_answer(data)
    messages.append({
        "role": "assistant", "text": data.get("answer", ""), "payload": data,
    })
