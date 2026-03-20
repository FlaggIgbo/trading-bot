#!/usr/bin/env python3
"""
Streamlit research UI (Gnosis agent_app.py style).

Pick ticker, optional multi-strategy compare, run compare_historical --preview --no-llm.

Usage (from repo root):
  streamlit run scripts/streamlit_research_app.py

Or:
  PYTHONPATH=. streamlit run scripts/streamlit_research_app.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

try:
    import streamlit as st
except ImportError:
    print("Install streamlit: pip install streamlit", file=sys.stderr)
    raise SystemExit(1)

from models.strategy_registry import list_strategies
from models.trader_agent import RUNNABLE_AGENTS


def _run_preview(
    *,
    ticker: str,
    compare_strategies: str,
    use_volatility: bool,
    markets_cap: int,
    days: int,
) -> tuple[int, str, str]:
    cmd = [
        sys.executable,
        str(_ROOT / "scripts" / "compare_historical.py"),
        "--preview",
        "--no-llm",
        "--days",
        str(days),
    ]
    if markets_cap > 0:
        cmd.extend(["--markets", str(markets_cap)])
    if use_volatility:
        cmd.append("--use-volatility")
    t = (ticker or "").strip()
    if t:
        cmd.extend(["--ticker", t])
    cs = (compare_strategies or "").strip()
    if cs:
        cmd.extend(["--compare-strategies", cs])
    env = {**os.environ, "PYTHONPATH": str(_ROOT)}
    p = subprocess.run(
        cmd,
        cwd=str(_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    out = (p.stdout or "") + ("\n--- stderr ---\n" + p.stderr if p.stderr else "")
    return p.returncode, out, " ".join(cmd)


def main() -> None:
    st.set_page_config(page_title="Trading bot research", layout="wide")
    st.title("Prediction market research")
    st.caption("Runs `compare_historical.py --preview --no-llm` (Gnosis / agent_app style).")

    with st.sidebar:
        st.subheader("DeployableTraderAgent registry")
        for name, cls in sorted(RUNNABLE_AGENTS.items()):
            st.text(f"{name} → {cls.__name__}")

    col1, col2 = st.columns(2)
    with col1:
        ticker = st.text_input(
            "Ticker filter (optional)",
            placeholder="e.g. KXBTC-25MAR2017 or Gemini ticker",
            help="Same as --ticker: evaluate only this market.",
        )
        days = st.number_input("Days of history", min_value=30, max_value=2000, value=365, step=30)
        markets_cap = st.number_input("Max markets per series (0 = all)", min_value=0, max_value=500, value=5)
    with col2:
        use_vol = st.checkbox("Use volatility model", value=True)
        default_cmp = "vol_regime,default_threshold,strict_threshold"
        compare_strategies = st.text_input(
            "Compare strategies (comma-separated)",
            value=default_cmp,
            help="Passed to --compare-strategies; same model P, different recommend() thresholds.",
        )

    if st.button("Run preview", type="primary"):
        with st.spinner("Running compare_historical…"):
            code, output, cmd = _run_preview(
                ticker=ticker,
                compare_strategies=compare_strategies,
                use_volatility=use_vol,
                markets_cap=int(markets_cap),
                days=int(days),
            )
        st.code(cmd, language="bash")
        if code != 0:
            st.error(f"Exit code {code}")
        else:
            st.success("Done")
        st.text_area("Output", output, height=600)

    with st.expander("Registered strategy_engine names"):
        st.write(list_strategies())


if __name__ == "__main__":
    main()
