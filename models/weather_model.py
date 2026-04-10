"""
Weather probability model: Mean + Variance temperature distribution.
Combines deterministic forecasts, ensemble spread, and seasonal baselines.
"""
from __future__ import annotations
import math
import re
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
    "KXLOWTAUS": "AUS",
    "KXLOWTDAL": "DAL",
    "KXLOWTPHL": "PHL",
    "KXLOWTPHIL": "PHL",
    "KXLOWTPHX": "PHX",
    "KXLOWTDEN": "DEN",
    "KXLOWTLAS": "LAS",
    "KXLOWTSEA": "SEA",
    "KXLOWTSFO": "SFO",
    "KXLOWTHOU": "HOU",
    "KXLOWTDC":  "DC",
    "KXLOWTATL": "ATL",
    "KXLOWTMSP": "MSP",
    "KXLOWTMSY": "MSY",
    "KXLOWTOKC": "OKC",
    "KXLOWTSAT": "SAT",
    "KXTEMPNYCH": "NYC",
}

# Add any Gemini specific keywords if needed, but compare_historical handles them via yf_ticker mapping

def get_weather_distribution(ticker: str, resolution_date: str) -> tuple[float, float] | None:
    """
    Returns (predicted_mean, predicted_std_dev) for a given weather ticker on resolution_date.
    resolution_date: ISO date "YYYY-MM-DD"
    """
    city_code = None
    # Lookup City Code (supports Kalshi prefix or Gemini keyword)
    for key, city in TICKER_CITY_MAP.items():
        if key in ticker.upper():
            city_code = city
            break
            
    if not city_code:
        # Fallback for Gemini keywords if not in TICKER_CITY_MAP
        for city in LOCATIONS.keys():
            if city in ticker.upper():
                city_code = city
                break
                
    if not city_code:
        return None
        
    lat, lon = LOCATIONS.get(city_code, (None, None))
    if lat is None:
        return None
        
    # Determine days to resolution
    try:
        res_dt = datetime.strptime(resolution_date, "%Y-%m-%d")
    except Exception:
        return None
    now_dt = datetime.utcnow()
    # Ensure non-negative and cap at 14 days for forecast window
    days_to_res = (res_dt - now_dt).days + (res_dt - now_dt).seconds / 86400.0
    days_to_res = max(0.01, min(14.0, days_to_res))

    # Determine if we want High/Max or Low/Min
    is_low = any(x in ticker.upper() for x in ("LOW", "MIN", "COLD"))
    
    # 1. Deterministic Forecast
    forecast = get_forecast(lat, lon)
    f_mean = None
    if forecast:
        if "TEMPNYCH" in ticker:
            # Parse hour from ticker: KXTEMPNYCH-26APR0316 or KXTEMPNYCH-26APR0316-T61
            hour_match = re.search(r'-(\d{2})([A-Z]{3})(\d{2})(\d{2})', ticker)
            if hour_match:
                #YY = hour_match.group(1), MMM = hour_match.group(2), DD = hour_match.group(3), HH = hour_match.group(4)
                target_hour = int(hour_match.group(4))
                hourly = forecast.get("hourly", {})
                h_times = hourly.get("time", [])
                h_temps = hourly.get("temperature_2m", [])
                
                # Match datetime "YYYY-MM-DDTHH:00"
                target_ts = f"{resolution_date}T{target_hour:02d}:00"
                if target_ts in h_times:
                    idx = h_times.index(target_ts)
                    f_mean = h_temps[idx]
        elif is_low:
            daily = forecast.get("daily", {})
            times = daily.get("time", [])
            if resolution_date in times:
                idx = times.index(resolution_date)
                f_mean = daily.get("temperature_2m_min", [])[idx]
        else:
            # Default to High/Max weather probability
            daily = forecast.get("daily", {})
            times = daily.get("time", [])
            if resolution_date in times:
                idx = times.index(resolution_date)
                f_mean = daily.get("temperature_2m_max", [])[idx]
                
    # 2. Ensemble Forecast (uncertainty proxy)
    ensemble = get_ensemble_forecast(lat, lon)
    e_std_dev = None
    if ensemble:
        hourly = ensemble.get("hourly", {})
        h_times = hourly.get("time", [])
        # Ensemble provides multiple members. Standard deviation across members for the target hour.
        # Find target hour index
        target_ts = f"{resolution_date}T12:00:00" # Mid-day proxy if specific hour not available
        if "TEMPNYCH" in ticker:
            hour_match = re.search(r'-(\d{2})([A-Z]{3})(\d{2})(\d{2})', ticker)
            if hour_match:
                target_hour = int(hour_match.group(4))
                target_ts = f"{resolution_date}T{target_hour:02d}:00"
        
        # Open-Meteo ensemble returns members as temperature_2m_member00, etc.
        member_temps = []
        for k, v in hourly.items():
            if "member" in k and target_ts in h_times:
                idx = h_times.index(target_ts)
                if idx < len(v) and v[idx] is not None:
                    member_temps.append(v[idx])
        
        if len(member_temps) > 1:
            mean_m = sum(member_temps) / len(member_temps)
            var_m = sum((m - mean_m)**2 for m in member_temps) / len(member_temps)
            e_std_dev = math.sqrt(var_m)
            # Add baseline floor for ensemble spread
            e_std_dev = max(1.5, e_std_dev)

    if e_std_dev is None:
        # Fallback to distance-based heuristic if ensemble fails
        if days_to_res <= 1.0:
            e_std_dev = 1.8 # Increased baseline uncertainty
        elif days_to_res <= 5.0:
            e_std_dev = 1.8 + (3.5 - 1.8) * (days_to_res - 1.0) / 4.0
        else:
            e_std_dev = 3.5 + (6.0 - 3.5) * (days_to_res - 5.0) / 5.0
        
    # 3. Seasonality
    month_day = resolution_date[5:] # "MM-DD"
    seasonality = get_historical_seasonality(lat, lon, month_day)
    s_mean, s_std_dev = None, None
    if seasonality:
        s_data = seasonality.get("min" if is_low else "max")
        if s_data:
            s_mean = s_data.get("mean")
            s_std_dev = s_data.get("std_dev") or s_data.get("std")

    # 4. Blending
    if days_to_res <= 1.5:
        w_f, w_s = 0.95, 0.05
    elif days_to_res <= 5:
        w_f, w_s = 0.75, 0.25
    else:
        w_f, w_s = 0.4, 0.6
        
    if f_mean is None and s_mean is None:
        return None
        
    if f_mean is not None and s_mean is not None:
        final_mean = w_f * f_mean + w_s * s_mean
        # Standard deviation also blended
        final_std_dev = w_f * e_std_dev + w_s * (s_std_dev or 5.0)
    elif f_mean is not None:
        final_mean = f_mean
        final_std_dev = e_std_dev
    else:
        final_mean = s_mean
        final_std_dev = s_std_dev or 5.0
        
    if days_to_res < 0.5:
        current_temp = get_snapshot(lat, lon)
        if current_temp is not None:
            # Anchor mean to current temp + small drift towards daily max/min
            if "HIGH" in ticker or not is_low:
                final_mean = max(final_mean, current_temp + 0.5)
                # Shrink variance as we get closer to settlement
                final_std_dev *= 0.6
            else:
                final_mean = min(final_mean, current_temp - 0.5)
                final_std_dev *= 0.6
    
    return final_mean, final_std_dev

def calculate_weather_probability(ticker: str, resolution_date: str, strike: float, direction: str, strike_hi: float | None = None) -> float | None:
    """
    direction: 'above' | 'below' | 'between'
    If 'between', strike_hi must be provided (or inferred if strike encodes a range).
    """
    # 1. Determine time to resolution
    try:
        res_dt = datetime.strptime(resolution_date, "%Y-%m-%d")
        days_to_res = (res_dt - datetime.utcnow()).days + (res_dt - datetime.utcnow()).seconds / 86400.0
    except Exception:
        days_to_res = 1.0

    dist = get_weather_distribution(ticker, resolution_date)
    if not dist:
        return None
        
    mean, std_dev = dist
    
    # 2. Hard Anti-Stupidity Filters for Weather
    # Weather is not fat-tailed like BTC. 4-sigma is basically impossible.
    # Skip if strike is > 5.5F from mean for short-dated trades.
    dist_from_mean = abs(strike - mean)
    if days_to_res < 2.0 and dist_from_mean > 5.5:
        return None # SKIP trade entirely
    if days_to_res < 5.0 and dist_from_mean > 8.0:
        return None # SKIP
        
    if std_dev <= 0:
        if direction == 'above': return 1.0 if mean > strike else 0.0
        if direction == 'below': return 1.0 if mean < strike else 0.0
        if direction == 'between' and strike_hi is not None:
            return 1.0 if strike <= mean <= strike_hi else 0.0
        return 0.0
        
    p = None
    if direction == 'above':
        p = 1 - norm_cdf(strike, loc=mean, scale=std_dev)
    elif direction == 'below':
        p = norm_cdf(strike, loc=mean, scale=std_dev)
    elif direction == 'between' and strike_hi is not None:
        p_lo = norm_cdf(strike, loc=mean, scale=std_dev)
        p_hi = norm_cdf(strike_hi, loc=mean, scale=std_dev)
        p = max(0.0, p_hi - p_lo)
    
    if p is None:
        return None
        
    # 3. Tail Cutoff: If model says < 2% or > 98%, it's likely a tail we don't want to bet on
    # given weather forecasts can have systemic biases.
    if p < 0.02 or p > 0.98:
        return None # SKIP
        
    return p
