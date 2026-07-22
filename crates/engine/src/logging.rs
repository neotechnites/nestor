//! Minimal logging: human line to stdout + a machine JSONL trade log.
//! The JSONL log is the live forward test — every decision is recorded.

use std::io::Write;

pub fn info(msg: impl AsRef<str>) {
    println!("[{}] {}", chrono::Utc::now().to_rfc3339(), msg.as_ref());
}

/// Append one event to logs/<file>. Adds a `ts` field.
pub fn record(file: &str, mut event: serde_json::Value) {
    if let Some(obj) = event.as_object_mut() {
        obj.insert(
            "ts".into(),
            serde_json::json!(chrono::Utc::now().to_rfc3339()),
        );
    }
    let _ = std::fs::create_dir_all("logs");
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(format!("logs/{file}"))
    {
        let _ = writeln!(f, "{event}");
    }
}
