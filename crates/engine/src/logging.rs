//! Minimal logging: human line to stdout + a machine JSONL trade log.
//! The JSONL log is the live forward test — every decision is recorded.

use std::io::Write;

pub fn info(msg: impl AsRef<str>) {
    println!("[{}] {}", chrono::Utc::now().to_rfc3339(), msg.as_ref());
}

/// Append one event to logs/<file>. Adds a `ts` field.
pub fn record(file: &str, event: serde_json::Value) {
    record_path(&format!("logs/{file}"), event);
}

/// Append one event to an explicit path (parent dirs created). Adds a `ts` field.
/// For records a spec pins to a location (e.g. `data/streak_week1.jsonl`).
pub fn record_path(path: &str, mut event: serde_json::Value) {
    if let Some(obj) = event.as_object_mut() {
        obj.insert(
            "ts".into(),
            serde_json::json!(chrono::Utc::now().to_rfc3339()),
        );
    }
    if let Err(e) = write_line(path, &event) {
        // A dropped trade record is dangerous (a real order with no local trace),
        // so scream to stderr with the full event rather than swallowing it.
        eprintln!("[log] FAILED writing to {path}: {e} — dropped event: {event}");
    }
}

fn write_line(path: &str, event: &serde_json::Value) -> std::io::Result<()> {
    if let Some(dir) = std::path::Path::new(path).parent() {
        if !dir.as_os_str().is_empty() {
            std::fs::create_dir_all(dir)?;
        }
    }
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    writeln!(f, "{event}")
}
