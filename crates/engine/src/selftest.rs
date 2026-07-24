//! T007 — live order-path self-test for the Kalshi V2 create-order endpoint
//! (`POST /trade-api/v2/portfolio/events/orders`). Proves RSA signing + V2 order
//! placement + synchronous fill-truth parsing + position read work against real
//! Kalshi, at trivial risk, BEFORE any strategy trades live. Live-only (needs API
//! keys). Validated by actually running it; the pure pieces it leans on
//! (`parse_balance`, side/price translation, response parsing) are unit-tested in
//! `kalshi.rs`.

use anyhow::{bail, Result};

use crate::kalshi::Kalshi;

/// Read balance, place ONE tiny IOC limit buy on `ticker` at `price_cents`
/// (YES side), print the RAW request-equivalent + RAW response, parse the
/// synchronous fill truth, then read positions. Deliberately manual: you pass the
/// exact ticker and price so nothing is auto-chosen.
///
/// The order is immediate_or_cancel: on an empty/uncrossable book it returns
/// fill_count 0 and the exchange has already canceled the remainder — that is a
/// PASS (we are proving request shape + auth + response parse, not fills).
pub async fn run(
    kalshi: &Kalshi,
    ticker: &str,
    price_cents: i64,
    count: i64,
    side: &str,
) -> Result<()> {
    if side != "yes" && side != "no" {
        bail!("side must be yes|no (got {side})");
    }
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
    // Show the exact V2 body we are about to POST (YES -> bid, price in dollars).
    let preview = serde_json::json!({
        "ticker": ticker,
        "side": crate::kalshi::book_side(side),
        "count": crate::kalshi::count_fp(count),
        "price": crate::kalshi::order_price_dollars(side, price_cents),
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": coid,
    });
    println!(
        "POST /trade-api/v2/portfolio/events/orders\nrequest body:\n{}",
        serde_json::to_string_pretty(&preview)?
    );

    let resp = kalshi
        .place_limit_buy(ticker, side, count, price_cents, &coid)
        .await?;
    println!("raw response:\n{}", serde_json::to_string_pretty(&resp)?);

    let placed = crate::kalshi::parse_place_response(&resp, side);
    println!("parsed placement: {placed:?}");
    println!(
        "  order_id={:?} fill_count={} remaining_count={} fill_price={:?}c actual_fee={:?}c ts_ms={:?}",
        placed.order_id,
        placed.fill_count,
        placed.remaining_count,
        placed.fill_price_cents,
        placed.actual_fee_cents,
        placed.ts_ms,
    );
    if placed.order_id.is_none() {
        println!("  (None order_id = SCHEMA SURPRISE — report it)");
    }
    if placed.fill_count == 0 {
        println!("  fill_count 0 on an IOC = uncrossable/empty book — request+auth+parse PASS.");
    }

    // Reconciliation cross-check: the fills API is still live in V2. Show BOTH the
    // raw JSON (schema truth) and what our parser extracted.
    let raw_fills = kalshi.fills(ticker).await?;
    let fills = crate::kalshi::parse_fills(&raw_fills, placed.order_id.as_deref(), side, 0);
    let (filled, avg, _) = crate::kalshi::fills_summary(&fills);
    println!("fills cross-check: parsed filled={filled} avg={avg:?}");
    println!("raw fills JSON:\n{}", serde_json::to_string_pretty(&raw_fills)?);

    let pos = kalshi.positions().await?;
    println!("positions:\n{}", serde_json::to_string_pretty(&pos)?);
    println!("self-test done — auth, signing, V2 order placement, response parse, positions read all worked.");
    Ok(())
}
