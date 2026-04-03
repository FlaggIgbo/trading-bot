"""
Weather probability model: Mean + Variance temperature distribution.
Combines deterministic forecasts, ensemble spread, and seasonal baselines.
"""
from __future__ import annotations
import math
from datetime import datetime

# Fallback Normal CDF using math.erf (avoids scipy dependency)
def norm_cdf(x, loc=0, scale=1):
    if scale <= 0:
        return 1.0 if x >= loc else 0.0
    return 0.5 * (1 + math.erf((x - loc) / (scale * math.sqrt(2))))

from signals.weather_client import LOCATIONS, get_forecast, get_ensemble_forecast, get_historical_seasonality, get_snapshot

# Ticker to City mapping (supports both Kalshi prefixes and Gemini keywords)
TICKER_CITY_MAP = {
    # Kalshi Highs
    "KXHIGHCHI": "CHI",
    "KXHIGHNYC": "NYC",
    "KXHIGHTNYC": "NYC",
    "KXHIGHTBOS": "BOS",
    "KXHIGHBOS": "BOS",
    "KXHIGHMIA": "MIA",
    "KXHIGHTMIA": "MIA",
    "KXHIGHLAX": "LAX",
    "KXHIGHTLAX": "LAX",
    "KXHIGHAUS": "AUS",
    "KXHIGHTAUS": "AUS",
    "KXHIGHDAL": "DAL",
    "KXHIGHTDAL": "DAL",
    "KXHIGHPHL": "PHL",
    "KXHIGHTPHL": "PHL",
    "KXHIGHPHX": "PHX",
    "KXHIGHTPHX": "PHX",
    "KXHIGHDEN": "DEN",
    "KXHIGHTDEN": "DEN",
    "KXHIGHSEA": "SEA",
    "KXHIGHTSEA": "SEA",
    "KXHIGHSFO": "SFO",
    "KXHIGHTSFO": "SFO",
    "KXHIGHHOU": "HOU",
    "KXHIGHTHOU": "HOU",
    "KXHIGHLAS": "LAS",
    "KXHIGHTLAS": "LAS",
    "KXHIGHDC":  "DC",
    "KXHIGHTDC":  "DC",
    "KXHIGHATL": "ATL",
    "KXHIGHMSP": "MSP",
    "KXHIGHMSY": "MSY",
    "KXHIGHOKC": "OKC",
    "KXHIGHSAT": "SAT",
    # Kalshi Lows
    "KXLOWTCHI": "CHI",
    "KXLOWTNYC": "NYC",
    "KXLOWTBOS": "BOS",
    "KXLOWTLAX": "LAX",
    "KXLOWTMIA": "MIA",
}

# Add any Gemini specific keywords if needed, but compare_historical handles them via yf_ticker mapping

def get_weather_distribution(ticker: str, resolution_date: str) -> tuple[float, float] | None:
    """
    Returns (predicted_mean, predicted_std_dev) for a given weather ticker on resolution_date.
    resolution_date: ISO date "YYYY-MM-DD"
    """
    city_code = None
    for t_prefix, city in TICKER_CITY_MAP.items():
        if ticker.startswith(t_prefix):
            city_code = city
            break
            
    if not city_code:
        return None
        
    lat, lon = LOCATIONS.get(city_code, (None, None))
    if lat is None:
        return None
        
    # 1. Deterministic Forecast
    forecast = get_forecast(lat, lon)
    f_mean = None
    if forecast:
        daily = forecast.get("daily", {})
        times = daily.get("time", [])
        if resolution_date in times:
            idx = times.index(resolution_date)
            # Use 'max' or 'min' based on ticker
            if "LOW" in ticker:
                f_mean = daily.get("temperature_2m_min", [])[idx]
            else:
                f_mean = daily.get("temperature_2m_max", [])[idx]
                
    # 2. Ensemble Forecast (uncertainty proxy)
    e_std_dev = 2.0 
        
    # 3. Seasonality
    month_day = resolution_date[5:] # "MM-DD"
    seasonality = get_historical_seasonality(lat, lon, month_day)
    s_mean, s_std_dev = None, None
    if seasonality:
        s_data = seasonality.get("min" if "LOW" in ticker else "max")
        if s_data:
            s_mean = s_data["mean"]
            s_std_dev = s_data["std_dev"]

    # 4. Blending
    try:
        res_dt = datetime.strptime(resolution_date, "%Y-%m-%d")
    except Exception:
        return None
    now_dt = datetime.utcnow()
    days_to_res = (res_dt - now_dt).days + (res_dt - now_dt).seconds / 86400.0
    
    if days_to_res <= 1:
        w_f, w_s = 0.9, 0.1
    elif days_to_res <= 5:
        w_f, w_s = 0.6, 0.4
    else:
        w_f, w_s = 0.3, 0.7
        
    if f_mean is None and s_mean is None:
        return None
        
    if f_mean is not None and s_mean is not None:
        final_mean = w_f * f_mean + w_s * s_mean
        final_std_dev = w_f * e_std_dev + w_s * (s_std_dev or 4.0)
    elif f_mean is not None:
        final_mean = f_mean
        final_std_dev = e_std_dev
    else:
        final_mean = s_mean
        final_std_dev = s_std_dev or 4.0
        
    if days_to_res < 0.5:
        current_temp = get_snapshot(lat, lon)
        if current_temp is not None:
            if "HIGH" in ticker:
                final_mean = max(final_mean, current_temp + 0.5)
                final_std_dev *= 0.7
    
    return final_mean, final_std_dev

def calculate_weather_probability(ticker: str, resolution_date: str, strike: float, direction: str, strike_hi: float | None = None) -> float | None:
    """
    direction: 'above' | 'below' | 'between'
    If 'between', strike_hi must be provided (or inferred if strike encodes a range).
    """
    dist = get_weather_distribution(ticker, resolution_date)
    if not dist:
        return None
        
    mean, std_dev = dist
    if std_dev <= 0:
        if direction == 'above': return 1.0 if mean > strike else 0.0
        if direction == 'below': return 1.0 if mean < strike else 0.0
        if direction == 'between' and strike_hi is not None:
            return 1.0 if strike <= mean <= strike_hi else 0.0
        return 0.0
        
    if direction == 'above':
        return 1 - norm_cdf(strike, loc=mean, scale=std_dev)
    elif direction == 'below':
        return norm_cdf(strike, loc=mean, scale=std_dev)
    elif direction == 'between' and strike_hi is not None:
        p_lo = norm_cdf(strike, loc=mean, scale=std_dev)
        p_hi = norm_cdf(strike_hi, loc=mean, scale=std_dev)
        return max(0.0, p_hi - p_lo)
    
    return None
