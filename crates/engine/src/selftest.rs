//! T007 — live order-path self-test. Proves RSA signing + order placement +
//! position read work against real Kalshi, at trivial risk, BEFORE any strategy
//! trades live. Live-only (needs API keys). This is validated by actually running
//! it (its whole purpose); the pure pieces it leans on (`parse_balance`, order
//! body) are unit-tested in `kalshi.rs`.

use anyhow::{bail, Result};

use crate::kalshi::Kalshi;

/// Read balance, place ONE tiny limit buy on `ticker` at `price_cents`, then read
/// positions to confirm it landed. Deliberately manual: you pass the exact ticker
/// and price so nothing is auto-chosen. Full settlement is exercised later by
/// `nestor reconcile`.
pub async fn run(kalshi: &Kalshi, ticker: &str, price_cents: i64, count: i64) -> Result<()> {
    if !(1..=99).contains(&price_cents) {
        bail!("price_cents must be 1..=99 (got {price_cents})");
    }
    if count < 1 {
        bail!("count must be >= 1");
    }

    let bal = kalshi.balance_cents().await?;
    println!("balance: ${:.2}", bal as f64 / 100.0);
    let cost = price_cents * count;
    if bal < cost {
        bail!("insufficient balance: need {cost}c, have {bal}c — fund the account first");
    }

    let coid = uuid::Uuid::new_v4().to_string();
    println!("placing {count} contract(s) of {ticker} @ {price_cents}c (client_order_id {coid})…");
    let resp = kalshi
        .place_limit_buy(ticker, "yes", count, price_cents, &coid)
        .await?;
    println!("order response:\n{}", serde_json::to_string_pretty(&resp)?);

    let pos = kalshi.positions().await?;
    println!("positions:\n{}", serde_json::to_string_pretty(&pos)?);
    println!("self-test done — auth, signing, order placement, and position read all worked.");
    Ok(())
}
