"""Weather-sleeve city config.

Tradeable-6 confirmed positive in the 2026-07-15 forward test; DEN/SEA are
negative controls (forecast MAE too high). `series` tickers and `station`
MUST be re-verified live against the Kalshi /events + IEM APIs before trading
(the forward-test agent saw a mix of KXHIGH<city> and KXHIGHT<city>).

`bias` = mean(raw_forecast - actual) over a trailing window, in deg F. Filled
by scripts/calibrate_bias.py; the placeholders below are the forward-window
estimates and are a starting point only.
"""

CITIES = {
    "MIA": {"series": "KXHIGHMIA",   "lat": 25.79, "lon": -80.29,  "station": "KMIA", "network": "FL_ASOS", "bias": 1.5, "tradeable": True},
    "ATL": {"series": "KXHIGHTATL",  "lat": 33.63, "lon": -84.44,  "station": "KATL", "network": "GA_ASOS", "bias": 1.5, "tradeable": True},
    "NY":  {"series": "KXHIGHNY",    "lat": 40.78, "lon": -73.97,  "station": "KNYC", "network": "NY_ASOS", "bias": 1.5, "tradeable": True},
    "BOS": {"series": "KXHIGHTBOS",  "lat": 42.36, "lon": -71.01,  "station": "KBOS", "network": "MA_ASOS", "bias": 1.5, "tradeable": True},
    "PHX": {"series": "KXHIGHTPHX",  "lat": 33.43, "lon": -112.00, "station": "KPHX", "network": "AZ_ASOS", "bias": 1.5, "tradeable": True},
    "CHI": {"series": "KXHIGHCHI",   "lat": 41.79, "lon": -87.75,  "station": "KMDW", "network": "IL_ASOS", "bias": 1.5, "tradeable": True},
    # Negative controls — do not trade (forecast unreliable)
    "DEN": {"series": "KXHIGHDEN",   "lat": 39.85, "lon": -104.66, "station": "KDEN", "network": "CO_ASOS", "bias": 1.5, "tradeable": False},
    "SEA": {"series": "KXHIGHTSEA",  "lat": 47.44, "lon": -122.31, "station": "KSEA", "network": "WA_ASOS", "bias": 1.5, "tradeable": False},
}

def tradeable_cities():
    return {k: v for k, v in CITIES.items() if v["tradeable"]}
