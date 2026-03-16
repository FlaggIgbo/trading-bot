I'll help you evaluate your `calculate_historical` script. Let me first find and examine it in your repository.Let me search more broadly for this script:Let me check the scripts directory:Now let me get the full `compare_historical.py` script to evaluate it thoroughly:Let me get the full `compare_historical.py` file to analyze it properly:Based on the image showing your trading results (100% losses across Bitcoin and SOL markets), I can see significant profitability issues. Let me pull up your full `compare_historical.py` script to analyze the core probability calculation and recommendation logic:Now let me get the specific probability calculation functions:Based on my review of your `compare_historical.py` script, I've identified several key areas where improvements could reduce trade losses:

## 1. **Overly Simple Empirical Probability Model**

Your `_empirical_probability()` function uses a basic fraction:
```python
return sum(1 for c in closes if c > threshold) / len(closes)
```

This treats all historical days equally, ignoring:
- **Recent market regime shifts** — old data from bull markets may not reflect current conditions
- **Volatility clustering** — you have volatility-based modeling (`_volatility_probability`), but it's not always blended with empirical data
- **Structural breaks** — market behavior may have fundamentally changed

**Recommendation:** 
- Weight recent closes more heavily (exponential decay or recency bias)
- Add a **regime detection** component to adjust for bull/bear markets
- Consider using a **rolling window** (e.g., last 90 days) instead of full historical period for faster adaptation

---

## 2. **Weak Volatility Blending**

Your code has `vol_blend_weight` but looking at the probability calculation flow, the blending logic appears optional and may not be consistently applied. The `_volatility_probability()` function uses Black-Scholes, but:
- It's only used when `--use-volatility` flag is passed
- The blend between empirical and volatility probabilities isn't visible in `_recommend()`

**Recommendation:**
- Make volatility blending **default behavior** (not opt-in)
- Implement a **dynamic blend** based on data quality: use more vol when you have few historical samples, more empirical when you have abundant clean data
- Add a **smile adjustment** — markets typically price out-of-the-money options higher than BS predicts

---

## 3. **Insufficient Edge Buffer for Model Uncertainty**

Your `_recommend()` function uses a fixed `min_edge`:
```python
net_edge_yes = historical_p_yes - market_yes_price - fee_yes
```

This doesn't account for:
- **Confidence intervals** — your model probability estimates have uncertainty bounds
- **Model drift** — how well your historical model actually predicts real outcomes
- **Calibration bias** — are your model probabilities actually accurate? (You do track calibration stats but may not be using them aggressively enough)

**Recommendation:**
- Compute a **confidence interval** around your probability estimate (e.g., ±5% or wider for thin data)
- Require edge to be **outside this interval** before buying
- Check `get_calibration_stats()` and adjust `min_edge` dynamically based on your historical accuracy

---

## 4. **P(loss) Blending Issue in `_compute_p_lose()`**

Your `_compute_p_lose()` blends model and market odds with a half-life decay:
```python
weight_market = sp.p_lose_blend_market_max_weight * (1.0 / (1.0 + dte / half))
```

But this might be **too aggressive toward market odds** late in contract life. Markets can be:
- Illiquid and manipulated (low volume on small exchanges)
- Biased toward extreme outcomes (tail risk hedging)

**Recommendation:**
- Add a **liquidity check**: if bid-ask spread is wide, weight market odds less
- Cap the maximum market weight — never let it override your model entirely
- Monitor whether trades placed near resolution actually confirm market odds

---

## 5. **Missing Stop-Loss Logic in Historical Model**

Your backtest shows **100% losses** on many predictions (e.g., Bitcoin and SOL trades all at -100%). This suggests:
- You're holding through resolution instead of cutting losses early
- Your model doesn't adjust when it sees contradictory price action
- The "sell losing position" logic (`enable_sell_and_rebuy`) might not be aggressive enough

**Recommendation:**
- Add **dynamic stop-loss**: if actual price moves opposite to your prediction early, exit at 50% loss instead of riding to 100%
- Track **intraday price movement** relative to threshold — if it's moving away, don't hold through resolution
- Implement **time-decay adjustment**: positions that age without confirming your thesis should be exited

---

## 6. **Threshold Extraction Fragility**

Your `_extract_strike()` function uses regex patterns and fallbacks:
```python
match = re.search(r"above\s*\$?([\d,]+(?:\.\d+)?)", title, re.I)
```

If the threshold is wrong, your entire probability calculation is meaningless. I see you have fallback logic, but:
- Kalshi ticker parsing (`-T([\d.]+)$`) assumes thresholds are always "above"
- Gemini extraction may miss edge cases

**Recommendation:**
- Cross-validate extracted thresholds against market data (is the market YES price reasonable for this strike?)
- Log extraction failures and manually review them
- Add explicit threshold validation — reject contracts where you're unsure

---

## 7. **No Slippage / Realistic Fill Modeling**

Your backtest uses `synthetic` mode with mid-price fills:
```python
price = float(snap.get("yes_bid") or 0) if side == "yes" else float(snap.get("no_bid") or 0)
if price <= 0:
    price = 0.5  # fallback
```

In reality:
- You fill at the ask (worse than mid)
- Your order may move the market
- Small positions may get partial fills

**Recommendation:**
- In backtest, use **ask prices instead of mid** for realistic slippage
- Account for **order size impact** — reduce position sizing if you see liquidity dry up
- Track **actual fills vs. theoretical** in your prediction records

---

## Summary: Top 3 Quick Wins

1. **Add recent-data weighting** to `_empirical_probability()` — give last 90 days 3x weight vs. older data
2. **Enable volatility blending by default** and require edge to exceed your model's confidence interval
3. **Implement dynamic stop-loss** in your trading logic — don't ride positions to -100% if the market is contradicting your thesis

Would you like me to create a GitHub issue to track these improvements, or would you like me to draft code changes for any of these recommendations?
