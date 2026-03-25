import math

import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta, timezone


def check_oil():
    tickers = ["CL=F", "BZ=F"]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365)
    
    for t in tickers:
        print(f"\nStats for {t} (last 365 days):")
        df = yf.download(t, start=start, end=end, interval="1d", progress=False)
        if df.empty:
            print("  No data found.")
            continue
        
        print(f"Columns: {df.columns}")
        print(df.head())
        # Flatten columns if multi-index
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        
        last_price = float(df['Close'].iloc[-1])
        min_price = float(df['Close'].min())
        max_price = float(df['Close'].max())
        avg_price = float(df['Close'].mean())
        
        print(f"  Current: {last_price:.2f}")
        print(f"  Min:     {min_price:.2f}")
        print(f"  Max:     {max_price:.2f}")
        print(f"  Average: {avg_price:.2f}")
        
        # Check thresholds
        wti_threshold = 98.99
        brent_threshold = 99.50
        
        if t == "CL=F":
            thresh = wti_threshold
        else:
            thresh = brent_threshold
            
        n = len(df)
        above_count = (df['Close'] > thresh).sum()
        pct_above = (float(above_count) / n) * 100
        print(f"  Days above {thresh}: {above_count} ({pct_above:.1f}%)")
        
        # Exponentially weighted P
        hl = 14
        lam = 0.693147 / hl
        ages = range(n - 1, -1, -1)
        weights = [math.exp(-lam * age) for age in ages]
        total_w = sum(weights)
        hits = [1.0 if c > thresh else 0.0 for c in df['Close']]
        weighted_p = sum(h * w for h, w in zip(hits, weights)) / total_w
        print(f"  Weighted P above {thresh}: {weighted_p:.1%}")

if __name__ == "__main__":
    check_oil()
