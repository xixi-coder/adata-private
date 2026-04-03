#!/usr/bin/env sh
set -eu

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
LINK_FILE="$BASE_DIR/reply_links.txt"
PIPELINE_PATH="${XHS_PIPELINE_PATH:-$BASE_DIR/xhs_pipeline.sh}"
RUN_AT="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="$BASE_DIR/pipeline_${RUN_AT}.log"

usage() {
  cat <<'USAGE'
Usage:
  sh run_from_last_link.sh

Behavior:
  - Read the last non-empty, non-comment line from reply_links.txt as m3u8 URL
  - Run xhs_pipeline.sh with that URL
  - Store output files (mp4/mp3/txt) in the same directory as this script

Optional env:
  XHS_PIPELINE_PATH  Override xhs_pipeline.sh path
USAGE
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

if [ ! -f "$LINK_FILE" ]; then
  cat > "$LINK_FILE" <<'TEMPLATE'
# Put one m3u8 URL per line.
# The script will use the last non-empty line each run.
TEMPLATE
  echo "Created: $LINK_FILE"
  echo "Please append your m3u8 URL, then run again."
  exit 1
fi

M3U8_URL="$(awk 'NF && $0 !~ /^[[:space:]]*#/' "$LINK_FILE" | tail -n 1 | tr -d '\r')"

if [ -z "$M3U8_URL" ]; then
  echo "Error: no valid m3u8 URL found in $LINK_FILE" >&2
  echo "Tip: append a URL on a new line, then rerun." >&2
  exit 1
fi

if [ ! -x "$PIPELINE_PATH" ]; then
  echo "Error: xhs_pipeline.sh not executable: $PIPELINE_PATH" >&2
  exit 1
fi

echo "Using URL: $M3U8_URL"
echo "Output directory: $BASE_DIR"
echo "Log file: $LOG_FILE"

if "$PIPELINE_PATH" "$M3U8_URL" "$BASE_DIR" 2>&1 | tee "$LOG_FILE"; then
  :
else
  echo "Error: pipeline failed. Check log: $LOG_FILE" >&2
  exit 1
fi

TXT_PATH="$(grep -E '^TXT:[[:space:]]+' "$LOG_FILE" | tail -n 1 | sed -E 's/^TXT:[[:space:]]*//')"
if [ -z "$TXT_PATH" ]; then
  TXT_PATH="$BASE_DIR/audio.mp3.txt"
fi

echo "DONE"
echo "TXT_PATH=$TXT_PATH"
echo "USED_URL=$M3U8_URL"
echo "LOG_FILE=$LOG_FILE"
