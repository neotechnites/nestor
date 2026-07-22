//! The Strategy trait + shared Engine context + the order-execution router.
//! New edges implement `Strategy` and route every order through `Engine::execute`,
//! which applies the Risk layer. Strategies never place raw orders themselves.

use std::sync::Mutex;

use crate::kalshi::Kalshi;
use crate::risk::{Order, Rejection, RiskManager, Signal};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// Log decisions, place no orders (but still simulate fills for accounting).
    Paper,
    /// Place real orders.
    Live,
}

impl Mode {
    pub fn from_env(s: &str) -> Self {
        if s.eq_ignore_ascii_case("live") {
            Mode::Live
        } else {
            Mode::Paper
        }
    }
}

/// Result of routing a signal through risk + execution.
#[derive(Debug)]
pub enum ExecOutcome {
    /// Live order placed and recorded.
    Filled {
        order: Order,
        response: serde_json::Value,
    },
    /// Paper mode: approved + recorded, no real order.
    Paper(Order),
    /// Risk layer refused.
    Rejected(Rejection),
    /// Live order placement errored.
    OrderError(String),
}

/// Shared context handed to every strategy run.
pub struct Engine {
    pub kalshi: Kalshi,
    pub http: reqwest::Client,
    pub mode: Mode,
    pub risk: Mutex<RiskManager>,
    pub cities: Vec<crate::config::City>,
}

impl Engine {
    /// Roll the risk layer's daily counters for `day` (ET, YYYY-MM-DD).
    pub fn begin_day(&self, day: &str) {
        self.risk.lock().unwrap().begin_day(day);
    }

    /// Route a signal through the Risk layer, then execute (live) or simulate
    /// (paper). Never holds the risk lock across the network await.
    pub async fn execute(&self, signal: Signal) -> ExecOutcome {
        let order = match self.risk.lock().unwrap().evaluate(&signal) {
            Ok(o) => o,
            Err(r) => return ExecOutcome::Rejected(r),
        };

        if self.mode == Mode::Live {
            let coid = uuid::Uuid::new_v4().to_string();
            match self
                .kalshi
                .place_limit_buy(
                    &order.ticker,
                    order.side.as_str(),
                    order.count,
                    order.limit_cents,
                    &coid,
                )
                .await
            {
                Ok(response) => {
                    self.risk.lock().unwrap().on_fill(&order);
                    ExecOutcome::Filled { order, response }
                }
                Err(e) => ExecOutcome::OrderError(e.to_string()),
            }
        } else {
            self.risk.lock().unwrap().on_fill(&order);
            ExecOutcome::Paper(order)
        }
    }
}

#[async_trait::async_trait]
pub trait Strategy {
    fn name(&self) -> &str;
    async fn run(&self, eng: &Engine) -> anyhow::Result<()>;
}
