#!/usr/bin/env bash
# Nightly compression of nestor observation logs (DATA CAPTURE, redirect 2026-07-23).
# Gzips dated obs logs (data/obs/YYYY-MM-DD.jsonl) STRICTLY older than today.
# Live files are never touched; already-compressed (.gz) files are skipped.
# Keep everything, delete nothing. Mirrors compress_old_obs_logs() in nestor_bin.
#
# Usage: run from the repo root (or anywhere — it resolves its own location).
#   scripts/compress_obs.sh
# Cron (05:00 daily):
#   0 5 * * * cd /path/to/nestor && scripts/compress_obs.sh >> data/obs/compress.log 2>&1
set -euo pipefail

# Repo root = parent of this script's dir, so cron can call it from anywhere.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OBS_DIR="${NESTOR_OBS_DIR:-$ROOT/data/obs}"
TODAY="$(date -u +%Y-%m-%d)"

[ -d "$OBS_DIR" ] || { echo "no obs dir at $OBS_DIR — nothing to do"; exit 0; }

shopt -s nullglob
n=0
for f in "$OBS_DIR"/*.jsonl; do
  base="$(basename "$f" .jsonl)"
  # Only dated files (YYYY-MM-DD) strictly older than today.
  [[ "$base" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue
  [[ "$base" < "$TODAY" ]] || continue
  gzip -f "$f"
  echo "compressed $f -> $f.gz"
  n=$((n + 1))
done
echo "compress_obs: $n file(s) compressed ($TODAY is live, left uncompressed)"
