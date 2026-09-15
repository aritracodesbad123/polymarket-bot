#!/usr/bin/env python3
"""POLYGROK Streamlit ops console — same live snapshot as the FastAPI UI."""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

# Allow `streamlit run streamlit_app.py` from ops-console/
import server as ops

st.set_page_config(
    page_title="POLYGROK Ops",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)

REFRESH_S = 4


@st.cache_data(ttl=REFRESH_S, show_spinner=False)
def load_snapshot(_bust: int) -> dict:
    return ops.build_snapshot()


def main() -> None:
    st.title("POLYGROK · Ops Console")
    st.caption(
        "Live mark-to-market from CLOB · paper-first bot · "
        f"DB `{ops.DB_PATH}`"
    )

    auto = st.sidebar.toggle("Auto-refresh", value=True)
    st.sidebar.caption(f"Poll every {REFRESH_S}s when on")
    if st.sidebar.button("Refresh now"):
        load_snapshot.clear()

    bust = int(time.time() // REFRESH_S)
    d = load_snapshot(bust)

    if d.get("error"):
        st.error(d["error"])
        return

    agent = d.get("agent") or {}
    marks = d.get("marks_live") or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cash", f"${d.get('cash', 0):.2f}")
    c2.metric(
        "Equity",
        f"${d.get('equity', 0):.2f}",
        delta=f"unreal {d.get('unrealized_pnl', 0):+.4f}",
    )
    kill = d.get("kill") or {}
    c3.metric("$ to kill", f"${kill.get('dollars_to_kill', 0):.2f}")
    c4.metric(
        "Fills",
        str(d.get("fills", 0)),
        delta=f"open {d.get('open_positions', 0)}",
    )

    st.sidebar.markdown("### Status")
    st.sidebar.write(
        f"**LaunchAgent:** {'UP' if agent.get('up') else 'DOWN'}  \n"
        f"**Mode:** {d.get('mode')}  \n"
        f"**Tip:** `{d.get('tip_commit')}`  \n"
        f"**Marks:** {'LIVE' if marks.get('ok') else 'DB'} "
        f"{marks.get('fetched', 0)}/{marks.get('open', 0)}  \n"
        f"**Query:** {d.get('query_ms')} ms  \n"
        f"**Snapshot:** `{str(d.get('ts', ''))[11:19]}`"
    )

    hist = d.get("equity_history") or []
    if hist:
        df_eq = pd.DataFrame(hist)
        df_eq["ts"] = pd.to_datetime(df_eq["ts"], utc=True)
        st.subheader("Equity live")
        st.line_chart(df_eq.set_index("ts")[["equity", "cash"]], height=280)
        start = float(d.get("start_bankroll") or 50)
        st.caption(
        f"Start bankroll ${start:.0f} · in market ${float(d.get('exposure') or 0):.2f}"
        + (
            f" · paper lock ~{int(d['paper_lock_remaining_s']) // 3600}h left"
            if d.get("paper_lock_remaining_s") is not None
            else ""
        )
    )

    st.subheader(
        f"Executed trades · MTM {'LIVE' if marks.get('ok') else 'DB'} "
        f"@ {str(d.get('ts', ''))[11:19]}"
    )
    trades = d.get("executed_trades") or []
    if trades:
        rows = []
        for t in trades:
            rows.append(
                {
                    "#": t.get("fill_id"),
                    "fill ts": str(t.get("ts") or "")[11:19],
                    "inst": t.get("instrument"),
                    "question": (t.get("question") or "")[:64],
                    "side": t.get("side"),
                    "entry": t.get("entry_price"),
                    "mark": t.get("mark"),
                    "bid": t.get("bid"),
                    "ask": t.get("ask"),
                    "unreal": t.get("unrealized"),
                    "mtm": str(t.get("mtm_ts") or "")[11:19] if t.get("mtm_live") else "—",
                    "size": t.get("size_usd"),
                    "status": t.get("status"),
                    "mode": t.get("mode"),
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No fills yet")

    left, right = st.columns(2)
    with left:
        st.subheader("Reject mix (2h)")
        mix = d.get("reject_mix") or {}
        if mix:
            st.bar_chart(pd.Series(mix, name="n"))
        else:
            st.caption("—")
        st.subheader("Edge funnel (2h)")
        fun = d.get("funnel") or {}
        st.write(
            {
                "candidates": fun.get("candidates"),
                "est OK": fun.get("estimates_ok"),
                "edge pass": fun.get("edge_pass"),
                "size pass": fun.get("size_pass"),
                "fills": fun.get("fills"),
            }
        )
    with right:
        st.subheader("Last rejects")
        rej = d.get("last_5_rejects") or []
        if rej:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "ts": str(r.get("ts") or "")[11:19],
                            "inst": r.get("instrument"),
                            "reason": r.get("reject_reason"),
                            "question": (r.get("question") or "")[:48],
                        }
                        for r in rej
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
        st.subheader("Provider")
        ps = d.get("provider_split") or {}
        st.write(
            f"Grok {ps.get('grok', 0)} · Gemini {ps.get('gemini', 0)} · "
            f"cascade `{d.get('cascade_stage') or '—'}`"
        )

    with st.expander("stderr (launchd)"):
        st.code("\n".join((d.get("logs") or {}).get("err") or [])[-8000:] or "—")

    if auto:
        time.sleep(REFRESH_S)
        st.rerun()


if __name__ == "__main__":
    main()
