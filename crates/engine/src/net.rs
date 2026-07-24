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
/// Works even when the reqwest error was wrapped by `?`/`.context(..)`.
pub fn http_status(err: &anyhow::Error) -> Option<u16> {
    for cause in err.chain() {
        if let Some(re) = cause.downcast_ref::<reqwest::Error>() {
            if let Some(s) = re.status() {
                return Some(s.as_u16());
            }
        }
    }
    None
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
}
