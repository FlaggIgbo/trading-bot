"""
Signals layer: data source clients for market data ingestion.

Provides API clients for Kalshi, Gemini, Yahoo Finance, and LLM-based analysis.
These are the INPUT layer of the ML pipeline — they fetch raw market signals
that feed into the models layer for feature engineering and prediction.

Submodules:
    signals.kalshi_client   - Kalshi exchange API client
    signals.gemini_client   - Gemini prediction markets API client
    signals.yfinance_client - Yahoo Finance historical/live data
    signals.ollama_analysis - LLM-powered trade analysis
"""
