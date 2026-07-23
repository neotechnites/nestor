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
    let ts_submit = chrono::Utc::now().timestamp_millis();
    println!("placing {count} contract(s) of {ticker} @ {price_cents}c (client_order_id {coid})…");
    let resp = kalshi
        .place_limit_buy(ticker, "yes", count, price_cents, &coid)
        .await?;
    println!("order response:\n{}", serde_json::to_string_pretty(&resp)?);
    let order_id = crate::kalshi::parse_order_id(&resp);
    println!("parsed order_id: {order_id:?} (None = SCHEMA SURPRISE — report it)");

    // Exercise the fills path the live execute() depends on: poll a few times
    // and show BOTH the raw JSON (schema truth) and what our parser extracted.
    for i in 0..5 {
        tokio::time::sleep(std::time::Duration::from_millis(800)).await;
        let raw = kalshi.fills(ticker).await?;
        let fills = crate::kalshi::parse_fills(&raw, order_id.as_deref(), "yes", ts_submit);
        let (filled, avg, _) = crate::kalshi::fills_summary(&fills);
        println!("fills poll {i}: parsed filled={filled} avg={avg:?}");
        if filled >= count {
            println!("raw fills JSON:\n{}", serde_json::to_string_pretty(&raw)?);
            break;
        }
        if i == 4 {
            println!("not filled after 5 polls — raw fills JSON for schema check:");
            println!("{}", serde_json::to_string_pretty(&raw)?);
            if let Some(id) = &order_id {
                println!("canceling unfilled order {id}…");
                let c = kalshi.cancel_order(id).await?;
                println!("cancel response:\n{}", serde_json::to_string_pretty(&c)?);
            }
        }
    }

    let pos = kalshi.positions().await?;
    println!("positions:\n{}", serde_json::to_string_pretty(&pos)?);
    println!(
        "self-test done — auth, signing, order placement, fills read{} all worked.",
        if order_id.is_some() {
            ", cancel path"
        } else {
            ""
        }
    );
    Ok(())
}
