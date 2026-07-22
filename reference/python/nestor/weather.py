"""Weather data: Open-Meteo forecast (the signal) + IEM actuals (truth/bias).

The edge is the bias-corrected forecast bought early. Open-Meteo runs ~1.5 F
hot; we subtract each city's measured bias before mapping to a bucket.
"""
import requests

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
IEM_DAILY = "https://mesonet.agron.iastate.edu/api/1/daily.json"


def forecast_for(lat, lon, date_str, tz="America/New_York"):
    """Forecast daily-max + precip for a specific calendar date (YYYY-MM-DD),
    pinned so the run-time-of-day can't shift which day we read. Returns
    (max_temp_f, precip_sum).

    NOTE: verify at build time that this endpoint serves the 00Z morning run
    (not a later-updated one) when called ~9am ET.
    """
    r = requests.get(OPEN_METEO, params={
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,precipitation_sum",
        "start_date": date_str, "end_date": date_str,
        "timezone": tz,
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
    }, timeout=30)
    r.raise_for_status()
    d = r.json()["daily"]
    return float(d["temperature_2m_max"][0]), float(d["precipitation_sum"][0])


def actual_high(station, network, date_str):
    """Official daily max high (F) from IEM for a given date (YYYY-MM-DD).
    This is settlement-grade truth (== Kalshi result). Returns None if missing.
    """
    r = requests.get(IEM_DAILY, params={
        "station": station, "network": network,
        "sdate": date_str, "edate": date_str,
    }, timeout=30)
    r.raise_for_status()
    rows = r.json().get("data", [])
    if not rows:
        return None
    v = rows[0].get("max_temp_f")
    return None if v is None else float(v)
