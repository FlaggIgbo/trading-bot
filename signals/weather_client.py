"""
Open-Meteo client for weather prediction contracts.
No API key required for free tier.
Docs: https://open-meteo.com/en/docs/forecast-api
      https://open-meteo.com/en/docs/ensemble-api
"""
from __future__ import annotations
import requests
import time
from datetime import datetime, timedelta
import os

_DEBUG = os.getenv("WEATHER_DEBUG", "").lower() in ("1", "true", "yes")

# Location coordinates (lat, lon) for Kalshi & Gemini weather markets
LOCATIONS = {
    "NYC": (40.7128, -74.0060),  # Central Park / NYC
    "CHI": (41.8781, -87.6298),  # Midway / Chicago
    "MIA": (25.7617, -80.1918),  # Miami International
    "LAX": (34.0522, -118.2437), # Los Angeles / LAX
    "BOS": (42.3601, -71.0589),  # Logan / Boston
    "AUS": (30.2672, -97.7431),  # Austin
    "DAL": (32.7767, -96.7970),  # Dallas
    "PHL": (39.9526, -75.1652),  # Philadelphia
    "DC":  (38.9072, -77.0369),  # Washington DC
    "ATL": (33.7490, -84.3880),  # Atlanta
    "HOU": (29.7604, -95.3698),  # Houston
    "PHX": (33.4484, -112.0740), # Phoenix
    "SFO": (37.7749, -122.4194), # San Francisco
    "SEA": (47.6062, -122.3321), # Seattle
    "DEN": (39.7392, -104.9903), # Denver
    "MSP": (44.9778, -93.2650),  # Minneapolis
    "MSY": (29.9511, -90.0715),  # New Orleans
    "OKC": (35.4676, -97.5164),  # Oklahoma City
    "SAT": (29.4241, -98.4936),  # San Antonio
    "LAS": (36.1716, -115.1391), # Las Vegas
    "SAN": (32.7157, -117.1611), # San Diego (fallback)
}

CACHE_DIR = os.path.expanduser("~/.trading-bot-data/weather_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

def _get_cache_key(prefix: str, lat: float, lon: float, date_str: str) -> str:
    return f"{prefix}_{lat}_{lon}_{date_str}.json"

def get_forecast(lat: float, lon: float) -> dict | None:
    """Get current forecast (up to 7-16 days out)."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if _DEBUG:
            print(f"[weather_client] Forecast failed for ({lat}, {lon}): {e}")
        return None

def get_ensemble_forecast(lat: float, lon: float) -> dict | None:
    """Get ensemble forecast (for uncertainty modelling)."""
    url = "https://ensemble-api.open-meteo.com/v1/ensemble"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",
        "models": "gfs_seamless", # High resolution US-focused ensemble
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if _DEBUG:
            print(f"[weather_client] Ensemble failed for ({lat}, {lon}): {e}")
        return None

def get_historical_seasonality(lat: float, lon: float, date_of_year: str) -> dict | None:
    """Get historical averages for a specific day of the year (roughly)."""
    url = "https://archive-api.open-meteo.com/v1/archive"
    
    current_year = datetime.now().year
    
    cache_key = _get_cache_key("seasonality", lat, lon, date_of_year)
    cache_path = os.path.join(CACHE_DIR, cache_key)
    
    if os.path.exists(cache_path):
        import json
        with open(cache_path, "r") as f:
            return json.load(f)

    start_date = f"{current_year-20}-01-01"
    end_date = f"{current_year-1}-12-31"
    
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",
    }
    
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        
        daily = data.get("daily", {})
        times = daily.get("time", [])
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        
        target_maxes = []
        target_mins = []
        
        for i, t in enumerate(times):
            if t.endswith(f"-{date_of_year}"):
                if max_temps[i] is not None:
                    target_maxes.append(max_temps[i])
                if min_temps[i] is not None:
                    target_mins.append(min_temps[i])
        
        if not target_maxes:
            return None
            
        import math
        def stats(vals):
            if not vals: return None
            mean = sum(vals) / len(vals)
            var = sum((v - mean)**2 for v in vals) / len(vals)
            return {"mean": mean, "std_dev": math.sqrt(var), "n": len(vals)}
            
        res = {
            "max": stats(target_maxes),
            "min": stats(target_mins),
            "date": date_of_year,
            "lat": lat,
            "lon": lon
        }
        
        import json
        with open(cache_path, "w") as f:
            json.dump(res, f)
        return res
        
    except Exception as e:
        if _DEBUG:
            print(f"[weather_client] Seasonality failed for ({lat}, {lon}): {e}")
        return None

def get_snapshot(lat: float, lon: float) -> float | None:
    """Get current temperature."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current_weather": True,
        "temperature_unit": "fahrenheit",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("current_weather", {}).get("temperature")
    except Exception:
        return None


def get_recent_history(lat: float, lon: float, days: int = 31) -> dict | None:
    """Get recent historical temperatures (up to 31 past days)."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "past_days": days,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data.get("daily", {})
    except Exception as e:
        if _DEBUG:
            print(f"[weather_client] Recent history failed for ({lat}, {lon}): {e}")
        return None

