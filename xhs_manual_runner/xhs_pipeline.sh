#!/usr/bin/env sh
set -eu

usage() {
  cat <<'EOF'
Usage:
  ./xhs_pipeline.sh <m3u8_url> [output_dir]

Environment variables:
  WHISPER_MODEL_PATH   Whisper model path (default auto-detect: ~/Downloads/ggml-medium.bin)
  WHISPER_LANG         Whisper language (default: zh)
  WHISPER_THREADS      Whisper threads (default: CPU core count)
  FAST_AUDIO_ONLY      1=skip mp4 and only generate mp3 for fastest pipeline (default: 0)

Auto env loading:
  The script will auto source .evn.local or .env.local in current dir/script dir.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

if [ $# -lt 1 ]; then
  usage
  exit 1
fi

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: command not found: $1" >&2
    exit 1
  fi
}

hash_text() {
  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "$1" | shasum -a 256 | awk '{print substr($1, 1, 16)}'
  elif command -v md5 >/dev/null 2>&1; then
    printf '%s' "$1" | md5 | awk '{print substr($NF, 1, 16)}'
  elif command -v md5sum >/dev/null 2>&1; then
    printf '%s' "$1" | md5sum | awk '{print substr($1, 1, 16)}'
  else
    echo "Error: no hash command found (need shasum, md5, or md5sum)." >&2
    exit 1
  fi
}

require_cmd ffmpeg
require_cmd whisper-cli

INPUT_ARG="$1"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

load_env_if_exists() {
  f="$1"
  if [ -f "$f" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$f"
    set +a
    return 0
  fi
  return 1
}

# Support both names in case of typo: .evn.local / .env.local
if ! load_env_if_exists "$PWD/.evn.local"; then
  load_env_if_exists "$PWD/.env.local" || true
fi

URL="$INPUT_ARG"
OUT_DIR="${2:-./xhs_output_$(date +%Y%m%d_%H%M%S)}"
URL_ID="$(hash_text "$URL")"

mkdir -p "$OUT_DIR"
VIDEO_FILE="$OUT_DIR/output_${URL_ID}.mp4"
AUDIO_FILE="$OUT_DIR/audio_${URL_ID}.mp3"
URL_FILE="$OUT_DIR/source_${URL_ID}.url"
if [ -n "${WHISPER_MODEL_PATH:-}" ]; then
  :
elif [ -f "$HOME/Downloads/ggml-medium.bin" ]; then
  WHISPER_MODEL_PATH="$HOME/Downloads/ggml-medium.bin"
elif [ -f "$HOME/下载/ggml-medium.bin" ]; then
  WHISPER_MODEL_PATH="$HOME/下载/ggml-medium.bin"
else
  WHISPER_MODEL_PATH="ggml-medium.bin"
fi
WHISPER_LANG="${WHISPER_LANG:-zh}"
if command -v getconf >/dev/null 2>&1; then
  CPU_CORES="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
else
  CPU_CORES="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
fi
WHISPER_THREADS="${WHISPER_THREADS:-$CPU_CORES}"
FAST_AUDIO_ONLY="${FAST_AUDIO_ONLY:-0}"

USER_AGENT="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
HEADERS="$(printf 'Referer: https://www.xiaohongshu.com/\r\nOrigin: https://www.xiaohongshu.com\r\nAccept: */*\r\n')"

if [ ! -f "$WHISPER_MODEL_PATH" ]; then
  echo "Error: Whisper model file not found: $WHISPER_MODEL_PATH" >&2
  exit 1
fi

echo "Config:"
echo "  Whisper model: $WHISPER_MODEL_PATH"
echo "  Whisper lang:  $WHISPER_LANG"
echo "  Whisper thds:  $WHISPER_THREADS"
echo "  Fast audio:    $FAST_AUDIO_ONLY"
echo "  Output dir:    $OUT_DIR"
echo "  URL id:        $URL_ID"

run_ffmpeg_with_retry() {
  retry_max="${FFMPEG_RETRY_MAX:-3}"
  retry_sleep="${FFMPEG_RETRY_SLEEP:-3}"
  attempt=1

  while [ "$attempt" -le "$retry_max" ]; do
    if "$@"; then
      return 0
    fi

    if [ "$attempt" -ge "$retry_max" ]; then
      return 1
    fi

    echo "ffmpeg failed on attempt $attempt/$retry_max, retrying in ${retry_sleep}s..." >&2
    sleep "$retry_sleep"
    attempt=$((attempt + 1))
  done
}

printf '%s\n' "$URL" > "$URL_FILE"

if [ -f "$AUDIO_FILE" ] && { [ "$FAST_AUDIO_ONLY" = "1" ] || [ -f "$VIDEO_FILE" ]; }; then
  echo "[1/2] Reusing existing media for URL id $URL_ID"
else
  if [ "$FAST_AUDIO_ONLY" = "1" ]; then
    echo "[1/2] Downloading and extracting audio directly (fast mode)..."
    run_ffmpeg_with_retry ffmpeg \
      -user_agent "$USER_AGENT" \
      -headers "$HEADERS" \
      -allowed_extensions ALL \
      -protocol_whitelist "file,http,https,tcp,tls,crypto" \
      -i "$URL" \
      -map 0:a:0 \
      -vn -acodec mp3 \
      "$AUDIO_FILE"
  else
    echo "[1/2] Downloading stream and extracting audio in one ffmpeg pass..."
    run_ffmpeg_with_retry ffmpeg \
      -user_agent "$USER_AGENT" \
      -headers "$HEADERS" \
      -allowed_extensions ALL \
      -protocol_whitelist "file,http,https,tcp,tls,crypto" \
      -i "$URL" \
      -map 0:v? -map 0:a? -c copy -bsf:a aac_adtstoasc \
      "$VIDEO_FILE" \
      -map 0:a:0 -vn -acodec mp3 \
      "$AUDIO_FILE"
  fi
fi

TXT_CANDIDATE_1="${AUDIO_FILE}.txt"
TXT_CANDIDATE_2="$OUT_DIR/audio_${URL_ID}.txt"

if [ -f "$TXT_CANDIDATE_1" ] || [ -f "$TXT_CANDIDATE_2" ]; then
  echo "[2/2] Reusing existing transcript for URL id $URL_ID"
else
  echo "[2/2] Running whisper-cli..."
  whisper-cli -m "$WHISPER_MODEL_PATH" -f "$AUDIO_FILE" -l "$WHISPER_LANG" -t "$WHISPER_THREADS" -otxt
fi

if [ -f "$TXT_CANDIDATE_1" ]; then
  TRANSCRIPT_TXT="$TXT_CANDIDATE_1"
elif [ -f "$TXT_CANDIDATE_2" ]; then
  TRANSCRIPT_TXT="$TXT_CANDIDATE_2"
else
  echo "Error: transcript txt file not found after whisper-cli run." >&2
  echo "Checked: $TXT_CANDIDATE_1 and $TXT_CANDIDATE_2" >&2
  exit 1
fi

echo "Done."
echo "URL_ID:  $URL_ID"
echo "URL:     $URL"
if [ -f "$VIDEO_FILE" ]; then
  echo "Video:   $VIDEO_FILE"
fi
echo "Audio:   $AUDIO_FILE"
echo "TXT:     $TRANSCRIPT_TXT"
