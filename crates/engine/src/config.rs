//! Static strategy config. City table for the weather sleeve.
//!
//! `series` + `station` MUST be re-verified live against Kalshi /events and IEM
//! before trading (forward test saw a KXHIGH<city> / KXHIGHT<city> mix). `bias`
//! (deg F, mean forecast - actual over a trailing window) is a placeholder until
//! the calibration job fills real values — it is the actual edge.

#[derive(Debug, Clone)]
pub struct City {
    pub code: &'static str,
    pub series: &'static str,
    pub lat: f64,
    pub lon: f64,
    pub station: &'static str,
    pub network: &'static str,
    pub bias: f64,
    pub tradeable: bool,
}

pub fn cities() -> Vec<City> {
    vec![
        City {
            code: "MIA",
            series: "KXHIGHMIA",
            lat: 25.79,
            lon: -80.29,
            station: "KMIA",
            network: "FL_ASOS",
            bias: 1.5,
            tradeable: true,
        },
        City {
            code: "ATL",
            series: "KXHIGHTATL",
            lat: 33.63,
            lon: -84.44,
            station: "KATL",
            network: "GA_ASOS",
            bias: 1.5,
            tradeable: true,
        },
        City {
            code: "NY",
            series: "KXHIGHNY",
            lat: 40.78,
            lon: -73.97,
            station: "KNYC",
            network: "NY_ASOS",
            bias: 1.5,
            tradeable: true,
        },
        City {
            code: "BOS",
            series: "KXHIGHTBOS",
            lat: 42.36,
            lon: -71.01,
            station: "KBOS",
            network: "MA_ASOS",
            bias: 1.5,
            tradeable: true,
        },
        City {
            code: "PHX",
            series: "KXHIGHTPHX",
            lat: 33.43,
            lon: -112.00,
            station: "KPHX",
            network: "AZ_ASOS",
            bias: 1.5,
            tradeable: true,
        },
        City {
            code: "CHI",
            series: "KXHIGHCHI",
            lat: 41.79,
            lon: -87.75,
            station: "KMDW",
            network: "IL_ASOS",
            bias: 1.5,
            tradeable: true,
        },
        // Negative controls — do not trade (forecast unreliable)
        City {
            code: "DEN",
            series: "KXHIGHDEN",
            lat: 39.85,
            lon: -104.66,
            station: "KDEN",
            network: "CO_ASOS",
            bias: 1.5,
            tradeable: false,
        },
        City {
            code: "SEA",
            series: "KXHIGHTSEA",
            lat: 47.44,
            lon: -122.31,
            station: "KSEA",
            network: "WA_ASOS",
            bias: 1.5,
            tradeable: false,
        },
    ]
}

pub fn tradeable_cities() -> Vec<City> {
    cities().into_iter().filter(|c| c.tradeable).collect()
}
