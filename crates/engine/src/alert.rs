//! Best-effort out-of-band alerting. POSTs `{"text": msg}` to `ALERT_WEBHOOK_URL`
//! (Slack/Discord/Telegram-webhook compatible) so trades, halts, and errors reach
//! you without logging into the box. No-op if the env var is unset. Never fails
//! the caller — alerting must not break trading.

/// Fire an alert if `ALERT_WEBHOOK_URL` is set. Awaited but swallows all errors.
pub async fn notify(http: &reqwest::Client, msg: &str) {
    let url = match std::env::var("ALERT_WEBHOOK_URL") {
        Ok(u) if !u.trim().is_empty() => u,
        _ => return,
    };
    let body = serde_json::json!({ "text": format!("[nestor] {msg}") });
    if let Err(e) = http.post(&url).json(&body).send().await {
        eprintln!("[alert] webhook post failed: {e}");
    }
}
