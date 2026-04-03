#!/usr/bin/env sh
set -eu

usage() {
  cat <<'EOF'
Usage:
  ./xhs_pipeline.sh <m3u8_url> [output_dir]
  ./xhs_pipeline.sh <transcript_txt_path> [summary_output_path]

Environment variables:
  WHISPER_MODEL_PATH   Whisper model path (default auto-detect: ~/Downloads/ggml-medium.bin)
  WHISPER_LANG         Whisper language (default: zh)
  WHISPER_THREADS      Whisper threads (default: CPU core count)
  FAST_AUDIO_ONLY      1=skip mp4 and only generate mp3 for fastest pipeline (default: 0)
  GROQ_API_KEY         Required for summary step (used by groqTest/llama.py config)
  SUMMARY_MODEL        Optional override for LLM model name

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
require_cmd python3

INPUT_ARG="$1"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SUMMARY_SCRIPT="$SCRIPT_DIR/groqTest/summarize_txt_with_llama.py"

if [ -x "$PWD/venv/bin/python" ]; then
  PYTHON_BIN="$PWD/venv/bin/python"
elif [ -x "$SCRIPT_DIR/venv/bin/python" ]; then
  PYTHON_BIN="$SCRIPT_DIR/venv/bin/python"
else
  PYTHON_BIN="python3"
fi

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

if [ -z "${GROQ_API_KEY:-}" ]; then
  if ! load_env_if_exists "$SCRIPT_DIR/.evn.local"; then
    load_env_if_exists "$SCRIPT_DIR/.env.local" || true
  fi
fi

MODE="full"
TRANSCRIPT_TXT=""

if [ -f "$INPUT_ARG" ]; then
  case "$INPUT_ARG" in
    *.txt) MODE="summary_only" ;;
  esac
fi

if [ "$MODE" = "summary_only" ]; then
  TRANSCRIPT_TXT="$INPUT_ARG"
  if [ -n "${2:-}" ]; then
    SUMMARY_FILE="$2"
  else
    SUMMARY_FILE="${TRANSCRIPT_TXT%.txt}.summary.txt"
  fi
  OUT_DIR="$(dirname "$SUMMARY_FILE")"
else
  URL="$INPUT_ARG"
  OUT_DIR="${2:-./xhs_output_$(date +%Y%m%d_%H%M%S)}"
  SUMMARY_FILE="$OUT_DIR/summary.txt"
fi

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

if [ "$MODE" = "full" ] && [ ! -f "$WHISPER_MODEL_PATH" ]; then
  echo "Error: Whisper model file not found: $WHISPER_MODEL_PATH" >&2
  exit 1
fi

echo "Config:"
echo "  Mode:          $MODE"
if [ "$MODE" = "full" ]; then
  echo "  Whisper model: $WHISPER_MODEL_PATH"
  echo "  Whisper lang:  $WHISPER_LANG"
  echo "  Whisper thds:  $WHISPER_THREADS"
  echo "  Fast audio:    $FAST_AUDIO_ONLY"
fi
echo "  Output dir:    $OUT_DIR"

if [ "$MODE" = "full" ]; then
  if [ "$FAST_AUDIO_ONLY" = "1" ]; then
    echo "[1/3] Downloading and extracting audio directly (fast mode)..."
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
    echo "[1/3] Downloading stream and extracting audio in one ffmpeg pass..."
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

  echo "[2/3] Running whisper-cli..."
  whisper-cli -m "$WHISPER_MODEL_PATH" -f "$AUDIO_FILE" -l "$WHISPER_LANG" -t "$WHISPER_THREADS" -otxt -osrt

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
else
  if [ ! -f "$TRANSCRIPT_TXT" ]; then
    echo "Error: transcript txt file not found: $TRANSCRIPT_TXT" >&2
    exit 1
  fi
fi

if [ "$MODE" = "full" ]; then
  echo "[3/3] Summarizing transcript with model from groqTest/llama.py..."
else
  echo "[1/1] Summarizing transcript with model from groqTest/llama.py..."
fi
if [ -n "${SUMMARY_MODEL:-}" ]; then
  "$PYTHON_BIN" "$SUMMARY_SCRIPT" \
    --input-txt "$TRANSCRIPT_TXT" \
    --output "$SUMMARY_FILE" \
    --model "$SUMMARY_MODEL"
else
  "$PYTHON_BIN" "$SUMMARY_SCRIPT" \
    --input-txt "$TRANSCRIPT_TXT" \
    --output "$SUMMARY_FILE"
fi

echo "Done."
if [ "$MODE" = "full" ] && [ -f "$VIDEO_FILE" ]; then
  echo "Video:   $VIDEO_FILE"
fi
if [ "$MODE" = "full" ]; then
  echo "Audio:   $AUDIO_FILE"
fi
echo "TXT:     $TRANSCRIPT_TXT"
echo "Summary: $SUMMARY_FILE"
