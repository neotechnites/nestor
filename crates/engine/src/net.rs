//! Network resilience helpers: HTTP error classification + exponential backoff.
//!
//! Live scan/reconcile loops must not hammer Kalshi through a rate-limit ban or a
//! server-class outage — a 1s retry loop only prolongs the ban through an entry
//! window. These are pure, unit-tested primitives the loops use to (a) recognize
//! a retryable status buried in an anyhow chain and (b) compute a capped
//! exponential backoff. Also recognizes 401 (signed-request auth failure) which
//! after a Mac sleep = clock skew: public data still flows, signed calls 401.

use std::time::Duration;

/// Walk the error chain and return the first `reqwest` HTTP status, if any.
/// Works even when the reqwest error was wrapped by `?`/`.context(..)`. Falls back
/// to parsing an "HTTP <code>" token out of the message so status is still
/// recoverable for errors we build by hand from a raw body (kalshi::text_or_error,
/// place-order failures) where the live `reqwest::Error` no longer exists.
pub fn http_status(err: &anyhow::Error) -> Option<u16> {
    for cause in err.chain() {
        if let Some(re) = cause.downcast_ref::<reqwest::Error>() {
            if let Some(s) = re.status() {
                return Some(s.as_u16());
            }
        }
    }
    status_in_message(&err.to_string())
}

/// Recover an HTTP status from an error message containing an "HTTP <code>" token
/// (the shape produced by our hand-built body errors). None if absent/implausible.
pub fn status_in_message(msg: &str) -> Option<u16> {
    let idx = msg.find("HTTP ")?;
    let rest = &msg[idx + "HTTP ".len()..];
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    digits
        .parse::<u16>()
        .ok()
        .filter(|&s| (100..600).contains(&s))
}

/// Parse a `Retry-After` header value into seconds-to-wait, relative to `now`.
/// Supports BOTH RFC forms: integer delta-seconds ("120") and an HTTP-date
/// ("Wed, 21 Oct 2026 07:28:00 GMT"). A date in the past clamps to 0 (retry now).
/// Returns None only when the value parses as neither form.
pub fn parse_retry_after(value: &str, now: chrono::DateTime<chrono::Utc>) -> Option<u64> {
    let v = value.trim();
    if let Ok(secs) = v.parse::<u64>() {
        return Some(secs);
    }
    // HTTP-date form (RFC 7231 §7.1.3 uses the RFC 5322 / IMF-fixdate shape).
    let dt = chrono::DateTime::parse_from_rfc2822(v).ok()?;
    let secs = (dt.with_timezone(&chrono::Utc) - now).num_seconds();
    Some(secs.max(0) as u64)
}

/// Recover an already-resolved Retry-After (whole seconds) from an error message
/// carrying a "retry-after-secs=<n>" token (attached by kalshi::text_or_error when
/// a 429 response included the header). Loops take max(this, exp-backoff).
pub fn retry_after_secs_in_message(msg: &str) -> Option<u64> {
    const KEY: &str = "retry-after-secs=";
    let idx = msg.find(KEY)?;
    let rest = &msg[idx + KEY.len()..];
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    digits.parse::<u64>().ok()
}

/// A retryable status: 429 (rate limit) or any 5xx (server-class).
pub fn is_retryable_status(status: u16) -> bool {
    status == 429 || (500..600).contains(&status)
}

/// Consecutive signed-request auth failures (clock-skew symptom after sleep).
pub fn is_auth_failure(status: u16) -> bool {
    status == 401
}

/// Capped exponential backoff in seconds for the Nth consecutive retryable error
/// (n = 1 → 2s, 2 → 4s, 3 → 8s, … capped at 60s). n = 0 (no error) → 0s.
pub fn backoff_delay_secs(consecutive: u32) -> u64 {
    if consecutive == 0 {
        return 0;
    }
    // 2 * 2^(n-1) = 2^n, saturating, capped at 60.
    let shifted = 1u64.checked_shl(consecutive).unwrap_or(u64::MAX);
    shifted.min(60)
}

/// [`backoff_delay_secs`] as a `Duration`.
pub fn backoff_delay(consecutive: u32) -> Duration {
    Duration::from_secs(backoff_delay_secs(consecutive))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_progression_caps_at_60() {
        assert_eq!(backoff_delay_secs(0), 0);
        assert_eq!(backoff_delay_secs(1), 2);
        assert_eq!(backoff_delay_secs(2), 4);
        assert_eq!(backoff_delay_secs(3), 8);
        assert_eq!(backoff_delay_secs(4), 16);
        assert_eq!(backoff_delay_secs(5), 32);
        // 2^6 = 64 -> capped to 60, and everything beyond stays 60 (no overflow).
        assert_eq!(backoff_delay_secs(6), 60);
        assert_eq!(backoff_delay_secs(7), 60);
        assert_eq!(backoff_delay_secs(100), 60);
    }

    #[test]
    fn retryable_and_auth_classification() {
        assert!(is_retryable_status(429));
        assert!(is_retryable_status(500));
        assert!(is_retryable_status(503));
        assert!(!is_retryable_status(200));
        assert!(!is_retryable_status(400));
        assert!(!is_retryable_status(401));
        assert!(is_auth_failure(401));
        assert!(!is_auth_failure(429));
    }

    #[test]
    fn http_status_none_for_plain_error() {
        let e = anyhow::anyhow!("not an http error");
        assert_eq!(http_status(&e), None);
    }

    #[test]
    fn http_status_recovered_from_message() {
        // Errors we build from a raw body carry the status as an "HTTP <code>"
        // token; http_status must still classify them for backoff.
        let e = anyhow::anyhow!("markets HTTP 429 request-id=abc: rate limited");
        assert_eq!(http_status(&e), Some(429));
        let e2 = anyhow::anyhow!("order placement HTTP 409 (request-id r): body");
        assert_eq!(http_status(&e2), Some(409));
        assert!(is_retryable_status(http_status(&e).unwrap()));
        // no token → None
        assert_eq!(status_in_message("plain old error"), None);
        // implausible code rejected
        assert_eq!(status_in_message("HTTP 99999"), None);
    }

    #[test]
    fn parse_retry_after_integer_seconds() {
        let now = chrono::Utc::now();
        assert_eq!(parse_retry_after("120", now), Some(120));
        assert_eq!(parse_retry_after("  0 ", now), Some(0));
        assert_eq!(parse_retry_after("not-a-number", now), None);
    }

    #[test]
    fn parse_retry_after_http_date() {
        // Anchor "now" so the delta is deterministic.
        let now = chrono::DateTime::parse_from_rfc2822("Wed, 21 Oct 2026 07:28:00 GMT")
            .unwrap()
            .with_timezone(&chrono::Utc);
        // 90s in the future.
        let future = "Wed, 21 Oct 2026 07:29:30 GMT";
        assert_eq!(parse_retry_after(future, now), Some(90));
        // A date in the past clamps to 0 (retry now), not None.
        let past = "Wed, 21 Oct 2026 07:27:00 GMT";
        assert_eq!(parse_retry_after(past, now), Some(0));
    }

    #[test]
    fn retry_after_secs_recovered_from_message() {
        let e = "markets HTTP 429 request-id=x retry-after-secs=42: {\"error\":{}}";
        assert_eq!(retry_after_secs_in_message(e), Some(42));
        assert_eq!(retry_after_secs_in_message("no token here"), None);
    }
}
