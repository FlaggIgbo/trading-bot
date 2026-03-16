"""
Shared constants for recommendation strings used across evaluation and execution.
"""
# Platform identifiers (avoid hardcoded "kalshi"/"gemini" strings)
PLATFORM_KALSHI = "kalshi"
PLATFORM_GEMINI = "gemini"

# Recommendation outcomes from _recommend / _recommend_strict
REC_SKIP = "SKIP"
REC_BUY_YES = "BUY YES"
REC_BUY_NO = "BUY NO"

# Arbitrage triggers
REC_ARB_INTRA = "ARB_INTRA"
REC_ARB_INTER = "ARB_INTER"

# Sell triggers (position evaluation)
REC_LIKELY_LOSE = "LIKELY_LOSE"
REC_RULE_VIOLATION = "RULE_VIOLATION"
