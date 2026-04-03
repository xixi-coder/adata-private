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

mkdir -p "$OUT_DIR"
VIDEO_FILE="$OUT_DIR/output.mp4"
AUDIO_FILE="$OUT_DIR/audio.mp3"
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

if [ "$FAST_AUDIO_ONLY" = "1" ]; then
  echo "[1/2] Downloading and extracting audio directly (fast mode)..."
  ffmpeg \
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
  ffmpeg \
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

echo "[2/2] Running whisper-cli..."
whisper-cli -m "$WHISPER_MODEL_PATH" -f "$AUDIO_FILE" -l "$WHISPER_LANG" -t "$WHISPER_THREADS" -otxt

TXT_CANDIDATE_1="${AUDIO_FILE}.txt"
TXT_CANDIDATE_2="${OUT_DIR}/audio.txt"

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
if [ -f "$VIDEO_FILE" ]; then
  echo "Video:   $VIDEO_FILE"
fi
echo "Audio:   $AUDIO_FILE"
echo "TXT:     $TRANSCRIPT_TXT"
